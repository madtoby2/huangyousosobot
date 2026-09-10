#!/usr/bin/env python3
"""Compatibility wrapper for the full Whos.tv daily-task runner."""
import requests

from whos_daily_tasks import BASE_URL, complete_daily_tasks, main


def run_signin(session, username: str, password: str) -> dict:
    """Keep the legacy sign-in API used by earlier deployments/tests."""
    if not username or not password:
        raise RuntimeError('WHOS_TV_USERNAME/WHOS_TV_PASSWORD not configured')
    login = session.post(
        f'{BASE_URL}/api/login', json={'username': username, 'password': password},
        headers={'Accept': 'application/json', 'Content-Type': 'application/json'}, timeout=20,
    )
    login.raise_for_status()
    payload = login.json()
    if payload.get('code') not in {0, 200, 200000}:
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
    return {'claimed_at': progress.get('claimed_at'),
            'consecutive_days': meta.get('consecutive_days', 0),
            'points_balance': profile.get('points_balance')}


if __name__ == '__main__':
    raise SystemExit(main())
