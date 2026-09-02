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


def extract_video_code(title: str) -> str:
    """从带发布组/清晰度信息的 BT 标题中提取标准番号。"""
    m = re.search(r'(?<![A-Z0-9])([A-Z]{2,10})[-_ ]?(\d{2,6})(?!\d)', title.upper())
    return f'{m.group(1)}-{m.group(2)}' if m else ''


def _parse_javdb_search(html_txt: str, limit: int = 5):
    """解析 JavDB 当前 movie-list 卡片结构。"""
    results = []
    pattern = re.compile(
        r'<div class="item">\s*<a href="([^"]+)" class="box" title="([^"]*)">(.*?)</a>\s*</div>',
        re.S,
    )
    for m in pattern.finditer(html_txt):
        href, attr_title, body = m.groups()
        code_m = re.search(r'<div class="video-title">\s*<strong>([^<]+)</strong>(.*?)</div>', body, re.S)
        if not code_m:
            continue
        code = h.unescape(code_m.group(1)).strip().upper()
        title_tail = h.unescape(re.sub(r'<[^>]+>', '', code_m.group(2))).strip()
        cover_m = re.search(r'<img[^>]*src="([^"]+)"', body, re.S)
        date_m = re.search(r'<div class="meta">\s*([^<]+)', body, re.S)
        results.append({
            'title': f'{code} {title_tail or h.unescape(attr_title).strip()}'.strip(),
            'code': code,
            'url': f'https://javdb.com{href}' if href.startswith('/') else href,
            'cover': h.unescape(cover_m.group(1)) if cover_m else '',
            'release_date': date_m.group(1).strip() if date_m else '',
            'source': 'javdb',
            'source_label': '🔍 JavDB',
            'has_detail': True,
        })
        if len(results) >= limit:
            break
    return results


def enrich_bt_result(item: dict) -> dict:
    """按番号给 Sukebei 结果补充 JavDB 封面和作品元数据。"""
    enriched = dict(item)
    code = extract_video_code(item.get('title') or '')
    if not code:
        return enriched
    enriched['code'] = code
    metadata = search_javdb(code, 5).get('results', [])
    match = next((x for x in metadata if (x.get('code') or '').upper() == code), None)
    if not match:
        return enriched
    enriched.update({
        'code': code,
        'cover': match.get('cover') or '',
        'release_date': match.get('release_date') or '',
        'metadata_title': match.get('title') or '',
        'metadata_url': match.get('url') or '',
    })
    return enriched


def search_javdb(keyword: str, limit: int = 5):
    """JavDB 搜索 - 番号/封面元数据补充。"""
    from urllib.parse import quote
    url = f'https://javdb.com/search?q={quote(keyword)}&f=all'
    try:
        r = _sess().get(url, timeout=25)
        r.raise_for_status()
    except Exception as e:
        return {'error': f'JavDB 请求失败: {e}', 'results': []}
    return {'results': _parse_javdb_search(r.text, limit)}


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
