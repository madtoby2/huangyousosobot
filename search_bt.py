#!/usr/bin/env python3
"""BT 搜索模块：sukebei.nyaa.si + javdb.com 多源聚合"""

import html as h
import random
import re
from datetime import datetime, timedelta, timezone

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
_session = None


def _sess():
    global _session
    if _session is None:
        _session = cffi.Session(impersonate='chrome')
        _session.headers.update({'User-Agent': UA})
    return _session


UTC8 = timezone(timedelta(hours=8))
SUKEBEI_HOME = 'https://sukebei.nyaa.si/'


def parse_sukebei_results(html_text: str):
    """解析 Sukebei 搜索页，保留上传时间戳和可排序的做种数。"""
    soup = BeautifulSoup(html_text, 'html.parser')
    results = []
    for row in soup.select('table tbody tr'):
        cells = row.select('td')
        title_link = row.select_one('td:nth-of-type(2) a[href^="/view/"]')
        magnet_link = row.select_one('a[href^="magnet:"]')
        if not title_link or not magnet_link or len(cells) < 6:
            continue
        view_match = re.search(r'/view/(\d+)', title_link.get('href', ''))
        if not view_match:
            continue

        date_cell = row.select_one('td[data-timestamp]')
        timestamp = None
        if date_cell:
            try:
                timestamp = int(date_cell.get('data-timestamp'))
            except (TypeError, ValueError):
                pass

        seeders_text = cells[5].get_text(' ', strip=True)
        seeders_match = re.search(r'\d[\d,]*', seeders_text)
        seeders_count = int(seeders_match.group(0).replace(',', '')) if seeders_match else 0
        results.append({
            'torrent_id': view_match.group(1),
            'title': h.unescape(title_link.get_text(' ', strip=True)),
            'magnet': magnet_link.get('href', ''),
            'seeders': seeders_text or '?',
            'seeders_count': seeders_count,
            'uploaded_ts': timestamp,
            'uploaded_at': date_cell.get_text(' ', strip=True) if date_cell else '',
            'url': f'https://sukebei.nyaa.si/view/{view_match.group(1)}',
            'source': 'sukebei',
            'source_label': '🌰 Sukebei',
            'has_detail': False,
        })
    return results


def _fetch_sukebei_page(keyword: str = '', sort: str = 'seeders', page: int = 1):
    """抓取 Sukebei 指定排序和页码；网络异常由调用方显式处理。"""
    response = _sess().get(
        SUKEBEI_HOME,
        params={'q': keyword, 's': sort, 'o': 'desc', 'p': page},
        timeout=25,
    )
    response.raise_for_status()
    return parse_sukebei_results(response.text)


def _as_utc8(now=None):
    if now is None:
        return datetime.now(UTC8)
    if now.tzinfo is None:
        return now.replace(tzinfo=UTC8)
    return now.astimezone(UTC8)


def _item_timestamp(item):
    try:
        value = item.get('uploaded_ts')
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _item_seeders(item):
    value = item.get('seeders_count')
    if value is not None:
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
    match = re.search(r'\d[\d,]*', str(item.get('seeders', '')))
    return int(match.group(0).replace(',', '')) if match else 0


def _dedupe_torrents(items):
    unique = []
    seen = set()
    for item in items:
        key = item.get('torrent_id') or item.get('magnet') or item.get('url')
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def daily_ranking(limit: int = 10, max_pages: int = 20, now=None):
    """按 UTC+8 今日上传筛选，按当前做种数排序；返回扫描完整性信息。"""
    local_now = _as_utc8(now)
    now_ts = int(local_now.timestamp())
    day_start = int(local_now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    today_items = []
    complete = False
    scanned_pages = 0

    for page in range(1, max(0, int(max_pages)) + 1):
        page_items = _fetch_sukebei_page('', sort='id', page=page)
        scanned_pages += 1
        if not page_items:
            complete = True
            break
        timestamps = []
        for item in page_items:
            timestamp = _item_timestamp(item)
            if timestamp is None:
                continue
            timestamps.append(timestamp)
            if day_start <= timestamp <= now_ts:
                today_items.append(item)
        if timestamps and min(timestamps) < day_start:
            complete = True
            break

    today_items = _dedupe_torrents(today_items)
    today_items.sort(
        key=lambda item: (_item_seeders(item), _item_timestamp(item) or 0,
                          int(item.get('torrent_id') or 0)),
        reverse=True,
    )
    return {
        'results': today_items[:max(0, int(limit))],
        'total_today': len(today_items),
        'date': local_now.date().isoformat(),
        'complete': complete,
        'scanned_pages': scanned_pages,
    }


def random_recommendation(lookback_days: int = 7, max_pages: int = 8, now=None):
    """从近 N 天上传的 Sukebei 条目中随机抽取一条。"""
    local_now = _as_utc8(now)
    now_ts = int(local_now.timestamp())
    cutoff = now_ts - max(1, int(lookback_days)) * 86400
    candidates = []
    complete = False
    scanned_pages = 0

    for page in range(1, max(0, int(max_pages)) + 1):
        page_items = _fetch_sukebei_page('', sort='id', page=page)
        scanned_pages += 1
        if not page_items:
            complete = True
            break
        timestamps = []
        for item in page_items:
            timestamp = _item_timestamp(item)
            if timestamp is None:
                continue
            timestamps.append(timestamp)
            if cutoff <= timestamp <= now_ts:
                candidates.append(item)
        if timestamps and min(timestamps) < cutoff:
            complete = True
            break

    candidates = _dedupe_torrents(candidates)
    return {
        'result': random.choice(candidates) if candidates else None,
        'candidate_count': len(candidates),
        'lookback_days': max(1, int(lookback_days)),
        'complete': complete,
        'scanned_pages': scanned_pages,
    }


def search_sukebei(keyword: str, limit: int = 10):
    """Sukebei Nyaa 搜索 - 免 CF，直接磁力。"""
    try:
        results = _fetch_sukebei_page(keyword, sort='seeders', page=1)
    except Exception as e:
        return {'error': f'Sukebei 请求失败: {e}', 'results': []}
    return {'results': results[:max(0, int(limit))]}


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
