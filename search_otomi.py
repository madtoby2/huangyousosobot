#!/usr/bin/env python3
"""OtomiGames 实时搜索模块（含封面缩略图 + 广告过滤）"""

import re
import html as h
import requests

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
SESSION = requests.Session()
SESSION.headers.update({'User-Agent': UA})

SKIP_THUMB = ['logo', 'banner', 'erolabs', 'advert', 'promo', 'favicon', 'avatar', 'widget', 'ad-']


def _is_ad(url: str) -> bool:
    low = url.lower()
    return any(kw in low for kw in SKIP_THUMB)


def _extract_thumb(html_txt: str) -> str:
    """提取游戏封面：优先 RJ 编号图，回退第一个非广告图"""
    imgs = re.findall(r'(?:src|data-src)="((?:https?:)?//[^"]*?wp-content/uploads/[^"]*?)"', html_txt, re.I)
    for url in imgs:
        if re.search(r'/RJ\d+[_\-\.]', url, re.I):
            return url
    for url in imgs:
        if not _is_ad(url):
            return url
    for m in re.finditer(r'(?:src|data-src)="((?:https?:)?//[^"]+?)"', html_txt):
        url = m.group(1)
        if 'wp-content/uploads' in url:
            continue
        if not _is_ad(url) and not url.startswith('data:') and 'gravatar' not in url:
            return url
    return ''


def search(keyword: str, limit: int = 10):
    url = f'https://otomi-games.com/?s={requests.utils.quote(keyword)}'
    try:
        r = SESSION.get(url, timeout=25)
        r.raise_for_status()
    except Exception as e:
        return {'error': f'搜索请求失败: {e}', 'results': []}

    html_txt = r.text
    results = []
    seen_titles = set()

    for m in re.finditer(r'<h[234][^>]*class="[^"]*post-title[^"]*"[^>]*>.*?<a[^>]*href="(https://otomi-games\.com/[^"]+)"[^>]*>(.*?)</a>', html_txt, re.S):
        href, title_raw = m.group(1), m.group(2)
        title = h.unescape(re.sub(r'<[^>]+>', '', title_raw)).strip()
        if not title or title.lower() in seen_titles:
            continue
        seen_titles.add(title.lower())
        local = html_txt[max(0, m.start() - 1500):m.end() + 100]
        thumb = _extract_thumb(local)
        results.append({
            'title': title, 'url': href, 'snippet': '', 'thumb': thumb,
            'source': 'otomi', 'source_label': '🌸 OtomiGames',
        })
        if len(results) >= limit:
            break

    if not results:
        for m in re.finditer(r'<h[234][^>]*>.*?<a[^>]*href="(https://otomi-games\.com/[^"]+)"[^>]*>(.*?)</a>', html_txt, re.S):
            href, title_raw = m.group(1), m.group(2)
            title = h.unescape(re.sub(r'<[^>]+>', '', title_raw)).strip()
            if not title or title.lower() in seen_titles:
                continue
            seen_titles.add(title.lower())
            local = html_txt[max(0, m.start() - 1500):m.end() + 100]
            thumb = _extract_thumb(local)
            results.append({
                'title': title, 'url': href, 'snippet': '', 'thumb': thumb,
                'source': 'otomi', 'source_label': '🌸 OtomiGames',
            })
            if len(results) >= limit:
                break

    return {'results': results}


def get_detail(url: str):
    try:
        r = SESSION.get(url, timeout=30)
        r.raise_for_status()
    except Exception as e:
        return {'error': f'详情页请求失败: {e}'}

    html_txt = r.text

    def _fm(p, t, d=''):
        mm = re.search(p, t, re.S)
        return mm.group(1).strip() if mm else d

    title = h.unescape(_fm(r'<h1[^>]*>(.*?)</h1>', html_txt))
    plain = re.sub(r'<[^>]+>', ' ', html_txt)

    info_title = h.unescape(_fm(r'Title\s*:\s*(.*?)(?:Original|Language|Developer|Released|18\+|DESCRIPTION)', plain))
    developer = h.unescape(_fm(r'Developer\s*:\s*(.*?)(?:Released|18\+|DESCRIPTION)', plain))
    desc = h.unescape(_fm(r'DESCRIPTION\s*(.*?)(?:Screenshots|LINK DOWNLOAD|DOWNLOAD|Install|Password|TAGS|Previous|Next)', plain))[:900]

    host_map = {
        'pixeldrain.com': 'Pixeldrain', 'gofile.io': 'GoFile', 'katfile.com': 'KatFile',
        'datanodes.to': 'Datanodes', 'megaup.net': 'MegaUp', 'workupload.com': 'WorkUpload',
        'drive.google.com': 'Google Drive', 'mediafire.com': 'MediaFire', 'qiwi.gg': 'Qiwi',
        'uploadhaven.com': 'UploadHaven', 'rapidgator.net': 'Rapidgator',
    }
    download_buttons = []
    for href in re.findall(r'href="(https?://[^"]+)"', html_txt):
        low = href.lower()
        for host, name in host_map.items():
            if host in low:
                download_buttons.append({'label': name, 'url': href})
                break

    seen_urls = set()
    unique_btns = []
    for b in download_buttons:
        if b['url'] not in seen_urls:
            seen_urls.add(b['url'])
            unique_btns.append(b)

    thumb = _extract_thumb(html_txt)

    return {
        'title': title, 'info_title': info_title, 'developer': developer, 'desc': desc,
        'thumb': thumb, 'download_buttons': unique_btns, 'url': url,
        'source': 'otomi', 'source_label': '🌸 OtomiGames',
    }


if __name__ == '__main__':
    import sys
    kw = sys.argv[1] if len(sys.argv) > 1 else 'kabopuri'
    res = search(kw)
    print(f'Search for "{kw}": {len(res["results"])} results')
    for r in res['results'][:5]:
        print(f'  - {r["title"][:50]} | thumb: {r.get("thumb","")[:70] or "(none)"}')
    if res['results']:
        d = get_detail(res['results'][0]['url'])
        print(f'Detail: {d.get("title")} | thumb: {d.get("thumb","")[:80]}')
