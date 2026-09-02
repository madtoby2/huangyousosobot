#!/usr/bin/env python3
"""Ryuugames 实时搜索模块（curl_cffi 过 CF + age_gate cookie + processing 按钮解析）"""

import re
import json
import html as h
import base64
from urllib.parse import quote
from curl_cffi import requests as cffi

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
_session = None

HOST_MAP = {
    'pixeldrain.com': 'Pixeldrain', 'gofile.io': 'GoFile', 'katfile.com': 'KatFile',
    'datanodes.to': 'Datanodes', 'megaup.net': 'MegaUp', 'workupload.com': 'WorkUpload',
    'drive.google.com': 'Google Drive', 'mediafire.com': 'MediaFire', 'qiwi.gg': 'Qiwi',
    'uploadhaven.com': 'UploadHaven', 'rapidgator.net': 'Rapidgator',
}

SKIP_THUMB = ['logo', 'banner', 'erolabs', '聯播網', 'channel', 'advert', 'promo',
              'ryuu_discord', 'widget-premium', 'advanced-search', 'favicon', 'avatar']


def _sess():
    global _session
    if _session is None:
        _session = cffi.Session(impersonate='chrome')
        _session.headers.update({'User-Agent': UA})
        _session.cookies.set('age_gate', '1', domain='.ryuugames.com')
    return _session


def _is_ad(url: str) -> bool:
    low = url.lower()
    return any(kw in low for kw in SKIP_THUMB)


def _extract_thumb_from_html(html_txt: str) -> str:
    imgs = re.findall(r'(?:src|data-img-url)="((?:https?:)?//[^"]*?wp-content/uploads/[^"]*?)"', html_txt, re.I)
    for url in imgs:
        if re.search(r'/RJ\d+[_\-\.]', url, re.I):
            return url
    for url in imgs:
        if not _is_ad(url) and not url.startswith('data:'):
            return url
    for m in re.finditer(r'(?:src|data-img-url)="((?:https?:)?//[^"]+?)"', html_txt):
        url = m.group(1)
        if 'wp-content/uploads' in url:
            continue
        if not _is_ad(url) and not url.startswith('data:') and 'gravatar' not in url:
            return url
    return ''


def _resolve_processing_action(page_url: str, action: dict) -> str:
    try:
        data = {
            'ryuu_sl_action': 'process',
            'post_id': action.get('post_id') or '',
            'shortcode_id': action.get('shortcode_id') or '',
            'link_key': action.get('link_key') or '',
        }
        r = _sess().post(
            'https://www.ryuugames.com/processing/',
            data=data,
            headers={'User-Agent': UA, 'Referer': page_url, 'X-Requested-With': 'XMLHttpRequest'},
            timeout=25,
        )
        if r.status_code >= 400:
            return ''
        m = re.search(r'name=["\']host["\'][^>]*value=["\']([^"\']+)', r.text, re.I)
        if not m:
            return ''
        host_val = h.unescape(m.group(1))
        raw = base64.b64decode(host_val + '===').decode('utf-8', 'ignore')
        obj = json.loads(raw)
        link = (obj.get('url') or '').replace('\\/', '/').strip()
        return h.unescape(link) if link.startswith('http') else ''
    except Exception:
        return ''


def _extract_thumb_from_local(local_html: str) -> str:
    m = re.search(r'data-img-url="([^"]+)"', local_html)
    if m and m.group(1).startswith('http'):
        return m.group(1)
    m = re.search(r'<img[^>]*src="(https?://[^"]*?wp-content/uploads[^"]*?)"', local_html)
    if m and not _is_ad(m.group(1)):
        return m.group(1)
    return ''


def search(keyword: str, limit: int = 10):
    url = f'https://www.ryuugames.com/?s={quote(keyword)}'
    try:
        r = _sess().get(url, timeout=25)
        r.raise_for_status()
    except Exception as e:
        return {'error': f'搜索请求失败: {e}', 'results': []}

    html_txt = r.text
    results = []
    seen = set()

    for m in re.finditer(r'<h[23][^>]*>.*?<a[^>]*href="(https://www\.ryuugames\.com/[^"]+)"[^>]*>(.*?)</a>', html_txt, re.S):
        href, title_raw = m.group(1), m.group(2)
        title = h.unescape(re.sub(r'<[^>]+>', '', title_raw)).strip()
        if not title or title.lower() in seen:
            continue
        seen.add(title.lower())
        local = html_txt[max(0, m.start() - 1500):m.end() + 100]
        thumb = _extract_thumb_from_local(local)
        results.append({
            'title': title, 'url': href, 'snippet': '', 'thumb': thumb,
            'source': 'ryuugames', 'source_label': '🐉 Ryuugames',
        })
        if len(results) >= limit:
            break
    return {'results': results}


def get_detail(url: str):
    try:
        r = _sess().get(url, timeout=30)
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

    # 下载按钮：直接链接（host_map 匹配）
    download_buttons = []
    for href in re.findall(r'href="(https?://[^"]+)"', html_txt):
        low = href.lower()
        for host, name in HOST_MAP.items():
            if host in low:
                download_buttons.append({'label': name, 'url': href})
                break

    # 处理按钮（ryuu-sl-link-btn），参照 multigame 的 get_text(' ') 方式提取 label
    actions = []
    for btn in re.finditer(r'<button[^>]*class="[^"]*ryuu-sl-link-btn[^"]*"[^>]*data-link-key="([^"]+)"[^>]*data-post-id="([^"]+)"[^>]*data-shortcode-id="([^"]+)"[^>]*>(.*?)</button>', html_txt, re.S):
        link_key, post_id, shortcode_id, inner = btn.groups()
        label = re.sub(r'<[^>]+>', '', inner).strip()
        label = h.unescape(label) if label else 'Download'
        actions.append({'link_key': link_key, 'post_id': post_id, 'shortcode_id': shortcode_id, 'label': label})

    label_counts = {}
    for action in actions:
        link = _resolve_processing_action(url, action)
        if not link:
            continue
        label = action.get('label') or 'Download'
        label_counts[label] = label_counts.get(label, 0) + 1
        shown = label
        # 重复标签或与直接链接冲突时加 link_key 区分
        if label_counts[label] > 1 or any(b.get('label') == label for b in download_buttons):
            lk = action.get('link_key') or str(label_counts[label])
            shown = f'{label} {lk}'
        download_buttons.append({'label': shown, 'url': link})

    # 去重
    seen_urls = set()
    unique_btns = []
    for b in download_buttons:
        if b['url'] not in seen_urls:
            seen_urls.add(b['url'])
            unique_btns.append(b)

    thumb = _extract_thumb_from_html(html_txt)

    return {
        'title': title, 'info_title': info_title, 'developer': developer, 'desc': desc,
        'thumb': thumb, 'download_buttons': unique_btns, 'url': url,
        'source': 'ryuugames', 'source_label': '🐉 Ryuugames',
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
        print(f'Detail: {d.get("title")} | {len(d.get("download_buttons",[]))} dl buttons')
        for b in d.get('download_buttons', [])[:8]:
            print(f'   {b["label"]}: {b["url"][:80]}')
