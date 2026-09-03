#!/usr/bin/env python3
"""Stable immutable identifiers shared by paid delivery components."""
from __future__ import annotations

import hashlib
from urllib.parse import urlsplit, urlunsplit


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
