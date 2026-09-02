#!/usr/bin/env python3
"""BT 搜索模块：sukebei.nyaa.si + javdb.com 多源聚合"""

import re
import html as h
from curl_cffi import requests as cffi

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
_session = None


def _sess():
    global _session
    if _session is None:
        _session = cffi.Session(impersonate='chrome')
        _session.headers.update({'User-Agent': UA})
    return _session


def search_sukebei(keyword: str, limit: int = 10):
    """Sukebei Nyaa 搜索 - 免 CF，直接磁力"""
    from urllib.parse import quote
    url = f'https://sukebei.nyaa.si/?q={quote(keyword)}&s=seeders&o=desc'
    try:
        r = _sess().get(url, timeout=25)
        r.raise_for_status()
    except Exception as e:
        return {'error': f'Sukebei 请求失败: {e}', 'results': []}

    rows = re.split(r'<tr class=', r.text)[1:]
    results = []

    for row in rows[:limit]:
        t = re.search(r'<a[^>]*href="/view/(\d+)"[^>]*>([^<]+)</a>', row)
        mag = re.search(r'href="(magnet:\?xt=urn:btih:[^"]+)"', row)
        seeds = re.search(r'<td[^>]*class="text-center"[^>]*>([0-9,]+)</td>', row)
        size = re.search(r'<td class="text-center">([^<]*)</td>', row)  # first text-center td

        if not t or not mag:
            continue

        title = h.unescape(t.group(2).strip())
        magnet = mag.group(1)

        results.append({
            'title': title,
            'magnet': magnet,
            'seeders': seeds.group(1) if seeds else '?',
            'url': f'https://sukebei.nyaa.si/view/{t.group(1)}',
            'source': 'sukebei',
            'source_label': '🌰 Sukebei',
            'has_detail': False,
        })

    return {'results': results}


def search_javdb(keyword: str, limit: int = 5):
    """JavDB 搜索 - 番号/元数据补充"""
    from urllib.parse import quote
    url = f'https://javdb.com/search?q={quote(keyword)}&f=all'
    try:
        r = _sess().get(url, timeout=25)
        r.raise_for_status()
    except Exception as e:
        return {'error': f'JavDB 请求失败: {e}', 'results': []}

    html = r.text
    results = []

    # javdb movie cards
    for m in re.finditer(r'<a class="item" href="([^"]+)"[^>]*>.*?<div class="title">([^<]*)</div>', html, re.S):
        href, title = m.group(1), h.unescape(m.group(2)).strip()
        # Extract cover
        cover_m = re.search(r'<img[^>]*src="([^"]*)"[^>]*class="cover"', m.group(0), re.S)
        cover = cover_m.group(1) if cover_m else ''

        results.append({
            'title': title or href.split('/')[-1],
            'url': f'https://javdb.com{href}' if href.startswith('/') else href,
            'cover': cover,
            'source': 'javdb',
            'source_label': '🔍 JavDB',
            'has_detail': True,
        })
        if len(results) >= limit:
            break

    return {'results': results}


def search(keyword: str, limit: int = 10):
    """多源 BT 搜索：sukebei + javdb"""
    sukebei_res = search_sukebei(keyword, limit)
    javdb_res = search_javdb(keyword, max(3, limit // 2))

    results = (sukebei_res.get('results', []) + javdb_res.get('results', []))[:limit]
    return {'results': results}


def get_detail(url: str):
    """获取详情（javdb 详情页：封面、番号、发片日等）"""
    try:
        r = _sess().get(url, timeout=25)
        r.raise_for_status()
    except Exception as e:
        return {'error': f'详情页请求失败: {e}'}

    html = r.text

    def _fm(p, t, d=''):
        m = re.search(p, t, re.S)
        return m.group(1).strip() if m else d

    title = _fm(r'<h2[^>]*class="title"[^>]*>([^<]+)', html) or _fm(r'<title>([^<]+)', html)
    code = _fm(r'識別碼[^:]*:[^<]*<[^>]*>([^<]+)', html) or _fm(r'uuid[^:]*:[^<]*<[^>]*>([^<]+)', html)
    release = _fm(r'發行日期[^:]*:[^<]*<[^>]*>([^<]+)', html)
    cover = _fm(r'<img[^>]*src="([^"]*?cover[^"]*)"[^>]*>', html) or _fm(r'<img[^>]*class="video-cover"[^>]*src="([^"]+)"', html)

    return {
        'title': title,
        'code': code,
        'release_date': release,
        'cover': cover,
        'url': url,
        'source': 'javdb',
        'source_label': '🔍 JavDB',
    }


if __name__ == '__main__':
    import sys
    kw = sys.argv[1] if len(sys.argv) > 1 else 'MIDV'
    res = search(kw)
    print(f'Search for "{kw}": {len(res["results"])} results')
    for r in res['results'][:5]:
        src = r.get('source_label', r.get('source', ''))
        if 'magnet' in r:
            print(f'  [{src}] {r["title"][:50]} | seeds={r.get("seeders","?")} | mag: {r["magnet"][:50]}...')
        else:
            print(f'  [{src}] {r["title"][:50]} | {r.get("url","")}')
