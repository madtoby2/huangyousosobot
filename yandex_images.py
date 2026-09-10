"""Yandex Images general reverse-search uploader."""
import logging
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)
UPLOAD_URL = ('https://yandex.com/images/search?rpt=imageview&format=json&request='
              '%7B%22blocks%22%3A%5B%7B%22block%22%3A%22b-page_type_search-by-image__link%22%7D%5D%7D')
TIMEOUT = 25


def _clean_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        query = [(key, value) for key, value in parse_qsl(parsed.query)
                 if not key.startswith('utm_') and not key.startswith('__cf_')]
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ''))
    except Exception:
        return url


def parse_sites(html: str) -> list[dict]:
    if not html:
        return []
    soup = BeautifulSoup(html, 'html.parser')
    output = []
    seen = set()
    for card in soup.select('.CbirSites-Item'):
        title_link = card.select_one('a.Link_view_default[href]')
        domain_link = card.select_one('a.CbirSites-ItemDomain[href]')
        thumb_link = card.select_one('a.Thumb[href]')
        if not title_link:
            continue
        url = _clean_url(title_link.get('href', ''))
        if not url or url in seen:
            continue
        seen.add(url)
        title = ' '.join(title_link.get_text(' ', strip=True).split())
        domain = (' '.join(domain_link.get_text(' ', strip=True).split())
                  if domain_link else urlsplit(url).netloc)
        output.append({
            'title': title or domain or '(无标题)',
            'domain': domain,
            'url': url,
            'image_url': thumb_link.get('href', '') if thumb_link else '',
        })
    return output


def parse_upload(data: dict) -> dict | None:
    try:
        params = data['blocks'][0]['params']
        cbir_id = params['cbirId']
        original = params.get('originalImageUrl', '')
    except (KeyError, IndexError, TypeError):
        return None
    return {
        'cbir_id': cbir_id,
        'original_image_url': original,
        'search_url': f"https://yandex.com/images/search?rpt=imageview&cbir_id={quote(cbir_id, safe='')}",
    }


def search(image_path: str) -> dict | None:
    try:
        suffix = Path(image_path).suffix.lower()
        mime = {'.png': 'image/png', '.webp': 'image/webp'}.get(suffix, 'image/jpeg')
        with open(image_path, 'rb') as image:
            response = requests.post(
                UPLOAD_URL,
                files={'upfile': ('blob', image, mime)},
                headers={'User-Agent': 'Mozilla/5.0'},
                timeout=TIMEOUT,
            )
        response.raise_for_status()
        result = parse_upload(response.json())
        if not result:
            return None
        sites_response = requests.get(
            result['search_url'], headers={'User-Agent': 'Mozilla/5.0'}, timeout=TIMEOUT)
        sites_response.raise_for_status()
        result['sites'] = parse_sites(sites_response.text)[:5]
        return result
    except Exception as exc:
        logger.warning('Yandex image search failed: %s', exc)
        return None
