#!/usr/bin/env python3
"""Stable immutable identifiers shared by paid delivery components."""
from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qs, urlsplit, urlunsplit


def canonical_url(url: str) -> str:
    parts = urlsplit((url or '').strip())
    scheme = parts.scheme.lower()
    host = parts.netloc.lower()
    path = parts.path or '/'
    if path != '/':
        path = path.rstrip('/') + '/'
    return urlunsplit((scheme, host, path, parts.query, ''))


def game_resource_id(source: str, url: str, version: str = '', download_url: str = '') -> str:
    """Identify one immutable artifact version, not only its catalogue page."""
    source_key = (source or '').strip().lower()
    canonical = canonical_url(url)
    if not source_key or not canonical.startswith(('http://', 'https://')):
        raise ValueError('source and HTTP(S) URL are required')
    identity = '\n'.join((canonical, (version or '').strip(), canonical_url(download_url)))
    digest = hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]
    return f'{source_key}:{digest}'


class InvalidMagnet(ValueError):
    pass


def magnet_info_hash(magnet: str) -> str:
    value = str(magnet or '').strip()
    if not value or len(value) > 4096:
        raise InvalidMagnet('invalid magnet link')
    parts = urlsplit(value)
    if parts.scheme.lower() != 'magnet':
        raise InvalidMagnet('magnet scheme required')
    values = parse_qs(parts.query, keep_blank_values=True).get('xt', [])
    for item in values:
        prefix = 'urn:btih:'
        if item.lower().startswith(prefix):
            info_hash = item[len(prefix):]
            if re.fullmatch(r'[A-Fa-f0-9]{40}|[A-Za-z2-7]{32}', info_hash):
                return info_hash.lower()
    raise InvalidMagnet('valid BTIH hash required')


def bt_resource_id(magnet: str) -> str:
    return 'bt:' + magnet_info_hash(magnet)
