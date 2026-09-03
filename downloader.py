#!/usr/bin/env python3
"""Direct, bounded download adapters with pinned-address SSRF defence."""
from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import shutil
import socket
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3 import HTTPConnectionPool, HTTPSConnectionPool
from curl_cffi import requests as cffi_requests
from curl_cffi.const import CurlOpt


class DownloadError(RuntimeError): pass
class UnsupportedDownloadHost(DownloadError): pass
class DownloadTooLarge(DownloadError): pass

PUBLIC_HOST_SUFFIXES = ('mediafire.com', 'pixeldrain.com')
REDIRECT_CODES = (301, 302, 303, 307, 308)


def _host_matches(host: str, suffix: str) -> bool:
    return host == suffix or host.endswith('.' + suffix)


def _resolve_public(url: str, allow_private_for_test: bool = False):
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username:
        raise UnsupportedDownloadHost('download URL must use HTTP(S) without credentials')
    host = parts.hostname.lower().rstrip('.')
    if not (allow_private_for_test and host in ('127.0.0.1', 'localhost')):
        if not any(_host_matches(host, suffix) for suffix in PUBLIC_HOST_SUFFIXES):
            raise UnsupportedDownloadHost(f'unsupported download host: {host}')
    port = parts.port or (443 if parts.scheme == 'https' else 80)
    try:
        addresses = sorted({item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
    except OSError as exc:
        raise DownloadError('download hostname cannot be resolved') from exc
    if not addresses:
        raise DownloadError('download hostname has no address')
    parsed = [ipaddress.ip_address(x) for x in addresses]
    if not allow_private_for_test and any(not x.is_global for x in parsed):
        raise UnsupportedDownloadHost('non-public download address rejected')
    return host, port, addresses[0]


def _validate_public_url(url: str, allow_private_for_test: bool = False):
    _resolve_public(url, allow_private_for_test)


class _PinnedAdapter(HTTPAdapter):
    """Connect to the DNS-validated IP while retaining Host, SNI and TLS hostname checks."""
    def __init__(self, host, port, address, scheme):
        self.pinned_host, self.pinned_port = host, port
        self.pinned_address, self.pinned_scheme = address, scheme
        super().__init__(max_retries=0)

    def get_connection(self, url, proxies=None):
        if self.pinned_scheme == 'https':
            return HTTPSConnectionPool(self.pinned_address, port=self.pinned_port,
                                       assert_hostname=self.pinned_host,
                                       server_hostname=self.pinned_host)
        return HTTPConnectionPool(self.pinned_address, port=self.pinned_port)

    def add_headers(self, request, **kwargs):
        host = self.pinned_host
        default = 443 if self.pinned_scheme == 'https' else 80
        request.headers['Host'] = host if self.pinned_port == default else f'{host}:{self.pinned_port}'


def _one_pinned_get(url: str, *, stream=False, timeout=(20, 120),
                    allow_private_for_test=False, headers=None):
    parts = urlsplit(url)
    host, port, address = _resolve_public(url, allow_private_for_test)
    session = requests.Session()
    session.trust_env = False
    session.mount(parts.scheme + '://', _PinnedAdapter(host, port, address, parts.scheme))
    try:
        response = session.get(url, stream=stream, timeout=timeout, allow_redirects=False,
                               headers=headers or {'User-Agent': 'Mozilla/5.0'})
    except Exception:
        session.close()
        raise
    response._pinned_session = session
    return response


def _cffi_one_pinned_get(url: str, *, stream=False, timeout=(20, 120),
                           allow_private_for_test=False, headers=None, impersonate='chrome'):
    parts = urlsplit(url)
    host, port, address = _resolve_public(url, allow_private_for_test)
    pinned = f'[{address}]' if ':' in address else address
    session = cffi_requests.Session(curl_options={CurlOpt.RESOLVE: [f'{host}:{port}:{pinned}']})
    try:
        response = session.get(url, stream=stream, timeout=timeout, allow_redirects=False,
                               headers=headers, impersonate=impersonate)
    except Exception:
        session.close(); raise
    response._pinned_session = session
    return response


def _close_response(response):
    if response is None: return
    response.close()
    session = getattr(response, '_pinned_session', None)
    if session: session.close()


def _secure_get(url: str, *, stream=False, timeout=(20, 120),
                allow_private_for_test=False, max_redirects=5, headers=None,
                impersonate=None):
    current = url
    for hop in range(max_redirects + 1):
        getter = _cffi_one_pinned_get if impersonate else _one_pinned_get
        response = getter(current, stream=stream, timeout=timeout,
                          allow_private_for_test=allow_private_for_test,
                          headers=headers, **({'impersonate': impersonate} if impersonate else {}))
        if response.status_code in REDIRECT_CODES and response.headers.get('Location'):
            if hop == max_redirects:
                _close_response(response)
                raise DownloadError('too many download redirects')
            next_url = urljoin(current, response.headers['Location'])
            _close_response(response)
            _validate_public_url(next_url, allow_private_for_test)
            current = next_url
            continue
        response.url = current
        return response
    raise DownloadError('redirect resolution failed')


def parse_mediafire_download(html: str) -> str:
    soup = BeautifulSoup(html or '', 'html.parser')
    button = soup.select_one('a#downloadButton[href]')
    if not button: raise DownloadError('MediaFire download button not found')
    url = button.get('href', '').strip()
    if not url.startswith(('http://', 'https://')):
        raise DownloadError('MediaFire returned an invalid download URL')
    host = (urlsplit(url).hostname or '').lower()
    if not _host_matches(host, 'mediafire.com'):
        raise DownloadError('MediaFire download escaped trusted host')
    return url


def resolve_download_url(url: str) -> str:
    parts = urlsplit((url or '').strip())
    host = (parts.hostname or '').lower().rstrip('.')
    if _host_matches(host, 'pixeldrain.com'):
        match = re.fullmatch(r'/u/([A-Za-z0-9]+)', parts.path.rstrip('/'))
        if not match: raise DownloadError('invalid PixelDrain share URL')
        return f'https://pixeldrain.com/api/file/{match.group(1)}?download'
    if _host_matches(host, 'mediafire.com'):
        response = _secure_get(url, timeout=(20, 30), impersonate='chrome')
        try:
            if response.status_code != 200:
                raise DownloadError(f'MediaFire page returned HTTP {response.status_code}')
            return parse_mediafire_download(response.text)
        finally:
            _close_response(response)
    raise UnsupportedDownloadHost(f'unsupported download host: {host or "missing"}')


def _safe_filename(response, url: str) -> str:
    disposition = response.headers.get('Content-Disposition', '')
    match = re.search(r"filename\*?=(?:UTF-8''|\")?([^\";]+)", disposition, re.I)
    raw = unquote(match.group(1).strip()) if match else unquote(Path(urlsplit(url).path).name)
    name = Path(raw).name.strip().replace('\x00', '')
    return name[:180] or 'download.bin'


def download_file(url: str, destination_dir: str, *, max_bytes: int = 2_000_000_000,
                  reserve_bytes: int = 1_073_741_824, allow_private_for_test: bool = False,
                  progress=None):
    if max_bytes <= 0 or reserve_bytes < 0: raise ValueError('invalid size limits')
    destination = Path(destination_dir); destination.mkdir(parents=True, exist_ok=True)
    response = _secure_get(url, stream=True, allow_private_for_test=allow_private_for_test)
    try:
        response.raise_for_status()
        length_text = response.headers.get('Content-Length')
        expected = int(length_text) if length_text and length_text.isdigit() else None
        if expected is not None and expected > max_bytes:
            raise DownloadTooLarge(f'file exceeds limit: {expected} bytes')
        required = (expected if expected is not None else max_bytes) + reserve_bytes
        if shutil.disk_usage(destination).free < required:
            raise DownloadError('insufficient disk space')
        final_path = destination / _safe_filename(response, response.url)
        part_path = Path(str(final_path) + '.part')
        digest = hashlib.sha256(); written = 0
        try:
            with part_path.open('xb') as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk: continue
                    written += len(chunk)
                    if written > max_bytes: raise DownloadTooLarge(f'file exceeds limit: {max_bytes} bytes')
                    if shutil.disk_usage(destination).free < len(chunk) + reserve_bytes:
                        raise DownloadError('disk reserve reached during download')
                    handle.write(chunk); digest.update(chunk)
                    if progress: progress(written, expected)
            if expected is not None and written != expected:
                raise DownloadError(f'incomplete download: {written}/{expected}')
            os.replace(part_path, final_path)
        except Exception:
            part_path.unlink(missing_ok=True); raise
        return {'path': str(final_path), 'file_size': written,
                'checksum': 'sha256:' + digest.hexdigest(), 'final_url': response.url}
    finally:
        _close_response(response)


def download_game_url(share_url: str, destination_dir: str, *, progress=None):
    return download_file(resolve_download_url(share_url), destination_dir, progress=progress)
