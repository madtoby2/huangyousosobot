#!/usr/bin/env python3
"""Complete Whos.tv daily tasks using the site's own action endpoints."""
from __future__ import annotations
import os
import time
from pathlib import Path
from urllib.parse import quote
from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests
from dotenv import load_dotenv
from whos_accounts import DEFAULT_ACCOUNTS_FILE, load_accounts
from whos_tv import BASE_URL

_SUCCESS_CODES = {0, 200, 200000}
_TASK_KEYS = ('daily_signin', 'task_frame_rating', 'task_favorite_content', 'task_share')


def _decode_html(text: str) -> str:
    slash = chr(92)
    return (text or '').replace(slash + 'n', '\n').replace(slash + '"', '"').replace(slash + "'", "'")


def parse_video_candidate(html_text: str, slug: str) -> dict | None:
    text = _decode_html(html_text)
    marker = "showRating('"
    start = text.find(marker)
    if start < 0:
        return None
    args = text[start + len(marker):].split(')', 1)[0]
    parts = args.split("'")
    quoted = [parts[0]] + parts[2::2]
    if len(quoted) < 5 or not quoted[0].isdigit() or not quoted[4].isdigit():
        return None
    video_id, my_rating = quoted[0], int(quoted[4])
    soup = BeautifulSoup(text, 'html.parser')
    favorite = any(tag.get('data-id') == video_id and tag.get('data-favorite') == 'true'
                   for tag in soup.select('[data-id][data-favorite]'))
    return {'slug': slug, 'id': video_id, 'my_rating': my_rating, 'is_favorite': favorite}


def _post_ok(session, path: str, *, data=None):
    url = f'{BASE_URL}{path}'
    # Site AJAX sends task actions as form data; login is JSON.
    response = session.post(url, json=data, timeout=30) if path == '/api/login' else session.post(url, data=data, timeout=30)
    response.raise_for_status()
    payload = response.json()
    if payload.get('code') not in _SUCCESS_CODES:
        raise RuntimeError(payload.get('message') or f'Whos.tv action failed: {path}')
    return payload


def _get_tasks(session) -> list[dict]:
    response = session.get(f'{BASE_URL}/api/user/tasks', timeout=30)
    response.raise_for_status()
    return ((response.json().get('data') or {}).get('daily') or [])


def _remaining(tasks: list[dict], key: str) -> int:
    item = next((x for x in tasks if x.get('task_key') == key), {})
    progress = item.get('progress') or {}
    maximum = int(item.get('max_completions') or item.get('target') or 0)
    return max(0, maximum - int(progress.get('today_completions') or 0))


def _listing_slugs(text: str) -> list[str]:
    soup = BeautifulSoup(_decode_html(text), 'html.parser')
    slugs = []
    for anchor in soup.select('a[href]'):
        href = anchor.get('href', '')
        if not href.startswith('/videos/'):
            continue
        slug = href[len('/videos/'):].split('/', 1)[0].split('?', 1)[0]
        if not slug or (slug.startswith('page-') and slug[5:].isdigit()):
            continue
        if slug not in slugs:
            slugs.append(slug)
    return slugs


def _video_candidates(session, rating_needed: int, favorite_needed: int = 0) -> list[dict]:
    """Find fresh video candidates for favorite tasks; rating tasks use frames."""
    if favorite_needed <= 0:
        return []
    first = session.get(f'{BASE_URL}/videos', timeout=40)
    first.raise_for_status()
    first_text = _decode_html(first.text)
    page_numbers = []
    soup = BeautifulSoup(first_text, 'html.parser')
    for anchor in soup.select('a[href]'):
        href = anchor.get('href', '')
        prefix = '/videos/page-'
        label = anchor.get_text(' ', strip=True)
        if href.startswith(prefix) and href[len(prefix):].isdigit():
            number = int(href[len(prefix):])
            if label == str(number) and 2 <= number <= 5 and number not in page_numbers:
                page_numbers.append(number)
    listing_urls = [f'{BASE_URL}/videos'] + [f'{BASE_URL}/videos/page-{n}' for n in sorted(page_numbers)]
    candidates, seen_slugs = [], set()
    for listing_url in listing_urls:
        if listing_url == f'{BASE_URL}/videos':
            listing_text = first_text
        else:
            listing = session.get(listing_url, timeout=40)
            listing.raise_for_status()
            listing_text = _decode_html(listing.text)
        for slug in _listing_slugs(listing_text):
            if slug in seen_slugs:
                continue
            seen_slugs.add(slug)
            if len(seen_slugs) > 60:
                return candidates
            page = session.get(f'{BASE_URL}/videos/{slug}', timeout=40)
            page.raise_for_status()
            item = parse_video_candidate(page.text, slug)
            if item and not item['is_favorite']:
                candidates.append(item)
                if len(candidates) >= favorite_needed:
                    return candidates
    return candidates


def _frame_candidates(session, rating_needed: int) -> list[dict]:
    """Collect unrated frames from the site's frame-explorer label pages."""
    if rating_needed <= 0:
        return []
    root = session.get(f'{BASE_URL}/frames', timeout=40)
    root.raise_for_status()
    soup = BeautifulSoup(_decode_html(root.text), 'html.parser')
    paths = []
    for anchor in soup.select('a[href]'):
        path = anchor.get('href', '')
        if path.startswith('/frames/type-') and '/label-' in path and path not in paths:
            paths.append(path)
    candidates, seen_ids = [], set()
    for path in paths[:75]:
        page = session.get(f'{BASE_URL}{path}', timeout=40)
        page.raise_for_status()
        frame_soup = BeautifulSoup(_decode_html(page.text), 'html.parser')
        for button in frame_soup.select('.frame-rate-btn'):
            frame_id = button.get('data-frame-id')
            my_rating = button.get('data-my-rating', button.get('data-rating-value', '0'))
            if frame_id and frame_id not in seen_ids and my_rating in ('', '0'):
                seen_ids.add(frame_id)
                candidates.append({'id': frame_id, 'my_rating': 0, 'path': path})
                if len(candidates) >= rating_needed:
                    return candidates
    return candidates


def complete_daily_tasks(session, username: str, password: str) -> dict:
    if not username or not password:
        raise RuntimeError('Whos.tv credentials not configured')
    _post_ok(session, '/api/login', data={'username': username, 'password': password})
    before = _get_tasks(session)
    rate_needed = _remaining(before, 'task_frame_rating')
    favorite_needed = _remaining(before, 'task_favorite_content')
    frames = _frame_candidates(session, rate_needed)
    videos = _video_candidates(session, 0, favorite_needed)
    if len(frames) < rate_needed:
        raise RuntimeError(f'not enough unrated frames: {len(frames)}/{rate_needed}')
    if len(videos) < favorite_needed:
        raise RuntimeError(f'not enough unfavorited videos: {len(videos)}/{favorite_needed}')
    if _remaining(before, 'daily_signin'):
        _post_ok(session, '/api/user/tasks/signin')
    for frame in frames[:rate_needed]:
        _post_ok(session, f"/api/frames/{frame['id']}/rate", data={'score': 5})
        time.sleep(0.3)
    for video in videos[:favorite_needed]:
        _post_ok(session, f"/api/videos/{video['id']}/favorite")
        time.sleep(0.3)
    for _ in range(_remaining(before, 'task_share')):
        _post_ok(session, '/api/user/tasks/task_share/complete')
        time.sleep(0.2)
    after = _get_tasks(session)
    summary, all_complete = {}, True
    for key in _TASK_KEYS:
        item = next((x for x in after if x.get('task_key') == key), {})
        progress = item.get('progress') or {}
        maximum = int(item.get('max_completions') or item.get('target') or 0)
        completed = int(progress.get('today_completions') or 0)
        summary[key] = {'completed': completed, 'maximum': maximum, 'status': progress.get('status')}
        all_complete = all_complete and maximum > 0 and completed >= maximum
    profile_response = session.get(f'{BASE_URL}/api/user/profile', timeout=30)
    profile_response.raise_for_status()
    profile = profile_response.json().get('data') or {}
    if not all_complete:
        raise RuntimeError(f'Whos.tv daily task verification failed: {summary}')
    return {'all_complete': True, 'tasks': summary, 'points_balance': profile.get('points_balance')}


def _failure_summary(exc: Exception, account: dict) -> str:
    """Keep actionable failure details while never logging account credentials."""
    message = f'{type(exc).__name__}: {exc}'.replace(chr(10), ' ')
    for key in ('username', 'password'):
        secret = str(account.get(key) or '')
        if secret:
            for variant in {secret, quote(secret, safe='')}:
                message = message.replace(variant, '<redacted>')
    return message[:240]


def main() -> int:
    load_dotenv(Path(__file__).with_name('.env'))
    accounts = load_accounts(os.environ.get('WHOS_TV_ACCOUNTS_FILE', DEFAULT_ACCOUNTS_FILE),
                             os.environ.get('WHOS_TV_USERNAME', '').strip(),
                             os.environ.get('WHOS_TV_PASSWORD', '').strip())
    if not accounts:
        raise RuntimeError('No Whos.tv accounts configured')
    failures = []
    for index, account in enumerate(accounts, start=1):
        session = cffi_requests.Session(impersonate='chrome')
        session.headers.update({'Accept': 'application/json', 'Accept-Language': 'zh-CN,zh;q=0.9'})
        try:
            result = complete_daily_tasks(session, account['username'], account['password'])
            print(f"TASKS_OK account={index}/{len(accounts)} points={result['points_balance']}")
        except Exception as exc:
            failures.append((index, exc))
            print(f'TASKS_FAILED account={index}/{len(accounts)} reason={_failure_summary(exc, account)}')
        finally:
            session.close()
    if failures:
        raise RuntimeError(f'{len(failures)} Whos.tv account(s) failed daily tasks')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
