#!/usr/bin/env python3
"""Whos.tv daily sign-in.

Whos.tv awards the daily sign-in when an authenticated login succeeds. This
script logs in, then verifies the daily_signin task is claimed before exiting
successfully. It never prints credentials.
"""
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

BASE_URL = 'https://whos.tv'
_SUCCESS_CODES = {0, 200, 200000}


def run_signin(session, username: str, password: str) -> dict:
    if not username or not password:
        raise RuntimeError('WHOS_TV_USERNAME/WHOS_TV_PASSWORD not configured')
    login = session.post(
        f'{BASE_URL}/api/login',
        json={'username': username, 'password': password},
        headers={'Accept': 'application/json', 'Content-Type': 'application/json'},
        timeout=20,
    )
    login.raise_for_status()
    payload = login.json()
    if payload.get('code') not in _SUCCESS_CODES:
        raise RuntimeError(payload.get('message') or 'Whos.tv login failed')

    tasks_response = session.get(f'{BASE_URL}/api/user/tasks', timeout=20)
    tasks_response.raise_for_status()
    daily = (tasks_response.json().get('data') or {}).get('daily') or []
    signin = next((item for item in daily if item.get('task_key') == 'daily_signin'), None)
    progress = (signin or {}).get('progress') or {}
    if progress.get('status') != 2 or not progress.get('claimed_at'):
        raise RuntimeError('Whos.tv daily sign-in was not claimed')

    profile_response = session.get(f'{BASE_URL}/api/user/profile', timeout=20)
    profile_response.raise_for_status()
    profile = profile_response.json().get('data') or {}
    meta = progress.get('meta') or {}
    return {
        'claimed_at': progress.get('claimed_at'),
        'consecutive_days': meta.get('consecutive_days', 0),
        'points_balance': profile.get('points_balance'),
    }


def main() -> int:
    load_dotenv(Path(__file__).with_name('.env'))
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.7',
        'Referer': f'{BASE_URL}/',
    })
    try:
        result = run_signin(
            session,
            os.environ.get('WHOS_TV_USERNAME', '').strip(),
            os.environ.get('WHOS_TV_PASSWORD', '').strip(),
        )
    finally:
        session.close()
    print(
        'SIGNIN_OK '
        f"claimed_at={result['claimed_at']} "
        f"streak={result['consecutive_days']} "
        f"points={result['points_balance']}"
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
