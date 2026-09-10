#!/usr/bin/env python3
"""Idempotently complete and verify Whos.tv daily account tasks."""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

from curl_cffi import requests as cffi_requests
from dotenv import load_dotenv

from whos_accounts import DEFAULT_ACCOUNTS_FILE, load_accounts
from whos_tv import BASE_URL

_SUCCESS_CODES = {0, 200, 200000}
_TASK_KEYS = ('daily_signin', 'task_frame_rating', 'task_favorite_content', 'task_share')


def _decode_html(text: str) -> str:
    return (text or '').replace('\\n', '\n').replace('\\"', '"').replace("\\'", "'")


def parse_video_candidate(html_text: str, slug: str) -> dict | None:
    text = _decode_html(html_text)
    match = re.search(
        r"showRating\('(\d+)',.*?,\s*'[^']*',\s*'[^']*',\s*'(\d+)'\)",
        text, re.DOTALL,
    )
    if not match:
        return None
    video_id, my_rating = match.groups()
    favorite = bool(re.search(
        rf'data-id="{re.escape(video_id)}"[^>]*data-favorite="true"', text, re.DOTALL
    ))
    return {'slug': slug, 'id': video_id, 'my_rating': int(my_rating),
            'is_favorite': favorite}


def _post_ok(session, path: str, *, data=None):
    response = session.post(f'{BASE_URL}{path}', json=data, timeout=30)
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


def _video_candidates(session, needed: int) -> list[dict]:
    if needed <= 0:
        return []
    response = session.get(f'{BASE_URL}/videos', timeout=40)
    response.raise_for_status()
    slugs = []
    for slug in re.findall(r'href="/videos/([^"/?#]+)', _decode_html(response.text)):
        if slug not in slugs:
            slugs.append(slug)
    candidates = []
    for slug in slugs[:60]:
        page = session.get(f'{BASE_URL}/videos/{slug}', timeout=40)
        page.raise_for_status()
        item = parse_video_candidate(page.text, slug)
        if item:
            candidates.append(item)
        if len(candidates) >= needed:
            break
    return candidates


def complete_daily_tasks(session, username: str, password: str) -> dict:
    if not username or not password:
        raise RuntimeError('Whos.tv credentials not configured')
    _post_ok(session, '/api/login', data={'username': username, 'password': password})
    before = _get_tasks(session)

    if _remaining(before, 'daily_signin'):
        _post_ok(session, '/api/user/tasks/signin')

    rate_needed = _remaining(before, 'task_frame_rating')
    favorite_needed = _remaining(before, 'task_favorite_content')
    candidates = _video_candidates(session, max(rate_needed, favorite_needed))

    rated = favorited = 0
    for item in candidates:
        if rated < rate_needed and item['my_rating'] == 0:
            _post_ok(session, f"/api/videos/{item['id']}/rate", data={'score': 5})
            rated += 1
        if favorited < favorite_needed and not item['is_favorite']:
            _post_ok(session, f"/api/videos/{item['id']}/favorite")
            favorited += 1
        if rated >= rate_needed and favorited >= favorite_needed:
            break
        time.sleep(0.3)
    if rated < rate_needed or favorited < favorite_needed:
        raise RuntimeError(
            f'not enough fresh video candidates: rating {rated}/{rate_needed}, '
            f'favorite {favorited}/{favorite_needed}'
        )

    for _ in range(_remaining(before, 'task_share')):
        _post_ok(session, '/api/user/tasks/task_share/complete')
        time.sleep(0.2)

    after = _get_tasks(session)
    summary = {}
    all_complete = True
    for key in _TASK_KEYS:
        item = next((x for x in after if x.get('task_key') == key), {})
        progress = item.get('progress') or {}
        maximum = int(item.get('max_completions') or item.get('target') or 0)
        completed = int(progress.get('today_completions') or 0)
        summary[key] = {'completed': completed, 'maximum': maximum,
                        'status': progress.get('status')}
        all_complete = all_complete and maximum > 0 and completed >= maximum

    profile_response = session.get(f'{BASE_URL}/api/user/profile', timeout=30)
    profile_response.raise_for_status()
    profile = profile_response.json().get('data') or {}
    if not all_complete:
        raise RuntimeError(f'Whos.tv daily task verification failed: {summary}')
    return {'all_complete': True, 'tasks': summary,
            'points_balance': profile.get('points_balance')}


def main() -> int:
    load_dotenv(Path(__file__).with_name('.env'))
    accounts = load_accounts(
        os.environ.get('WHOS_TV_ACCOUNTS_FILE', DEFAULT_ACCOUNTS_FILE),
        os.environ.get('WHOS_TV_USERNAME', '').strip(),
        os.environ.get('WHOS_TV_PASSWORD', '').strip(),
    )
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
            print(f'TASKS_FAILED account={index}/{len(accounts)} error={type(exc).__name__}')
        finally:
            session.close()
    if failures:
        raise RuntimeError(f'{len(failures)} Whos.tv account(s) failed daily tasks')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
