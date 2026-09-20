import ipaddress
import random
import re
import socket
from urllib.parse import quote, urlparse

import requests
from flask import Blueprint, Response, current_app, redirect, render_template, request, session, url_for

from configs.general_constants import USER_AGENT_PC
from configs.logging_config import get_logger
from src.api.access import consume_rate_limit, get_client_ip
from src.db import get_db, setting

logger = get_logger(__name__)

bp = Blueprint('web', __name__)

# 文件名缺省扩展名时按上游 Content-Type 推断（音频直链多为 mp3/m4a，路径通常不带扩展名）
CONTENT_TYPE_EXTENSIONS = {
    'audio/mpeg': 'mp3',
    'audio/mp3': 'mp3',
    'audio/mp4': 'm4a',
    'audio/x-m4a': 'm4a',
    'audio/aac': 'aac',
    'audio/wav': 'wav',
    'audio/x-wav': 'wav',
    'audio/ogg': 'ogg',
    'audio/flac': 'flac',
    'audio/x-flac': 'flac',
    'video/mp4': 'mp4',
    'video/quicktime': 'mov',
    'video/x-msvideo': 'avi',
    'video/webm': 'webm',
    'image/jpeg': 'jpg',
    'image/png': 'png',
    'image/gif': 'gif',
    'image/webp': 'webp',
    'image/avif': 'avif',
    'image/x-icon': 'ico',
}


def _is_safe_public_url(media_url):
    """仅放行解析到公网地址的 HTTP(S) 直链，防止下载代理被用于访问内网。"""
    try:
        parsed = urlparse(media_url)
        if parsed.scheme not in {'http', 'https'}:
            return False
        if parsed.username is not None or parsed.password is not None:
            return False
        if parsed.port not in {None, 80, 443}:
            return False
        hostname = (parsed.hostname or '').lower()
        if not hostname:
            return False
        addr_infos = socket.getaddrinfo(hostname, None)
    except (ValueError, socket.gaierror, OSError):
        return False
    if not addr_infos:
        return False
    for info in addr_infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if not ip.is_global:
            return False
    return True


def _sanitize_filename(raw):
    """清理文件名中跨平台文件系统的非法字符，并限制长度。"""
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]', ' ', str(raw or ''))
    text = re.sub(r'\s+', ' ', text).strip().strip('.').strip()
    return text[:100]


def _stream_media(upstream):
    """分块转发上游响应体，确保客户端中断时释放连接。"""
    try:
        for chunk in upstream.iter_content(chunk_size=64 * 1024):
            if chunk:
                yield chunk
    finally:
        upstream.close()


def _resolve_filename_extension(filename, content_type):
    """文件名不带扩展名时，根据上游 Content-Type 推断补全。"""
    if re.search(r'\.[A-Za-z0-9]{1,5}$', filename):
        return filename
    content_type = (content_type or '').split(';')[0].strip().lower()
    ext = CONTENT_TYPE_EXTENSIONS.get(content_type)
    if ext is None:
        ext = 'mp3' if content_type.startswith('audio/') else 'mp4'
    return f"{filename}.{ext}"


@bp.route('/download')
def download_video():
    """代理下载解析结果中的媒体直链，浏览器跨域直链无法用 download 属性命名文件。"""
    media_url = (request.args.get('url') or '').strip()
    if not media_url or not _is_safe_public_url(media_url):
        return {'success': False, 'message': '非法的下载地址'}, 400
    if not current_app.testing and not consume_rate_limit(f"download_ip:{get_client_ip()}", 10, window_seconds=60):
        return {'success': False, 'message': '下载过于频繁，请 1 分钟后再试'}, 429

    filename = _sanitize_filename(request.args.get('filename')) or '媒体文件'

    # 请求 identity 编码以保证上游 Content-Length 可直接转发
    upstream_headers = {
        'User-Agent': random.choice(USER_AGENT_PC),
        'Accept': '*/*',
        'Accept-Encoding': 'identity',
    }
    try:
        upstream = requests.get(media_url, headers=upstream_headers, stream=True, timeout=(5, 60))
    except requests.RequestException:
        logger.warning(f"Media download request failed: {media_url}")
        return {'success': False, 'message': '获取视频文件失败，链接可能已过期，请重新解析后再试'}, 500
    if upstream.status_code != 200:
        upstream.close()
        return {'success': False, 'message': '获取视频文件失败，链接可能已过期，请重新解析后再试'}, 500

    filename = _resolve_filename_extension(filename, upstream.headers.get('Content-Type'))
    # filename* 承载 UTF-8 原名，filename 兜底老客户端，仅保留 ASCII 主名与扩展名
    base, dot, ext = filename.rpartition('.')
    if not dot or not ext.isalnum():
        base, ext = filename, ''
    ascii_name = base.encode('ascii', 'ignore').decode().replace('"', '').strip().strip('.').strip() or 'media'
    ascii_name = f"{ascii_name}.{ext}" if ext else ascii_name
    disposition = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"

    response_headers = {'Content-Disposition': disposition}
    content_type = upstream.headers.get('Content-Type')
    if content_type:
        response_headers['Content-Type'] = content_type
    content_length = upstream.headers.get('Content-Length')
    if content_length:
        response_headers['Content-Length'] = content_length
    return Response(_stream_media(upstream), status=200, headers=response_headers)


@bp.route('/')
def index():
    """前台展示页面（Landing Page）"""
    if setting('homepage_enabled', '1') != '1':
        user_id = session.get('user_id')
        if user_id:
            user = get_db().execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
            if user:
                if user['role'] == 'admin':
                    return redirect(url_for('admin.overview'))
                return redirect(url_for('portal.overview'))
        return redirect(url_for('auth.login'))

    api_enabled = setting('global_api_enabled', '1') == '1'
    demo_enabled = setting('demo_enabled', '1') == '1'
    return render_template(
        'landing.html',
        api_enabled=api_enabled,
        demo_enabled=demo_enabled,
        parser_enabled=api_enabled and demo_enabled,
    )
