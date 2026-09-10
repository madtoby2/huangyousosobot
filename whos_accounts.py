#!/usr/bin/env python3
"""Whos.tv account registration, private storage and search pooling."""
from __future__ import annotations

import argparse
import json
import os
import secrets
import string
from pathlib import Path
from typing import Callable

from curl_cffi import requests as cffi_requests
from dotenv import load_dotenv

from whos_tv import BASE_URL, WhosTvClient

_SUCCESS_CODES = {0, 200, 200000}
DEFAULT_ACCOUNTS_FILE = str(Path(__file__).with_name('whos_accounts.json'))


class AccountStore:
    def __init__(self, path: str):
        self.path = Path(path)

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return []
        if not isinstance(data, list):
            return []
        return [
            {'username': str(item['username']), 'password': str(item['password'])}
            for item in data
            if isinstance(item, dict) and item.get('username') and item.get('password')
        ]

    def add(self, account: dict) -> bool:
        accounts = self.load()
        username = str(account.get('username') or '').strip()
        password = str(account.get('password') or '')
        if not username or not password:
            raise ValueError('username and password are required')
        if any(item['username'].lower() == username.lower() for item in accounts):
            return False
        accounts.append({'username': username, 'password': password})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f'.{self.path.name}.{secrets.token_hex(4)}.tmp')
        try:
            tmp.write_text(json.dumps(accounts, ensure_ascii=False, indent=2), encoding='utf-8')
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
            os.chmod(self.path, 0o600)
        finally:
            if tmp.exists():
                tmp.unlink()
        return True


def generate_credentials(prefix: str = 'sb') -> tuple[str, str]:
    alphabet = string.ascii_lowercase + string.digits
    username = prefix + ''.join(secrets.choice(alphabet) for _ in range(12))
    password = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(20)) + '!7'
    return username[:20], password


def register_account(session, username: str, password: str, *, invite_code: str = '') -> dict:
    payload = {'username': username, 'password': password}
    if invite_code:
        payload['invite_code'] = invite_code
    response = session.post(f'{BASE_URL}/api/register', json=payload, timeout=30)
    response.raise_for_status()
    data = response.json()
    if data.get('code') not in _SUCCESS_CODES:
        raise RuntimeError(data.get('message') or 'Whos.tv registration failed')
    profile_response = session.get(f'{BASE_URL}/api/user/profile', timeout=30)
    profile_response.raise_for_status()
    profile_payload = profile_response.json()
    if profile_payload.get('code') not in _SUCCESS_CODES or not profile_payload.get('data'):
        raise RuntimeError('Whos.tv registration could not be verified')
    return profile_payload['data']


def load_accounts(path: str, primary_username: str = '', primary_password: str = '') -> list[dict]:
    accounts = []
    if primary_username and primary_password:
        accounts.append({'username': primary_username, 'password': primary_password})
    for account in AccountStore(path).load():
        if not any(x['username'].lower() == account['username'].lower() for x in accounts):
            accounts.append(account)
    return accounts


class WhosAccountPool:
    def __init__(self, accounts: list[dict], *, client_factory: Callable = WhosTvClient):
        self.accounts = list(accounts)
        self.client_factory = client_factory

    def search(self, image_path: str) -> dict | None:
        errors = []
        for index, account in enumerate(self.accounts, start=1):
            client = self.client_factory(account['username'], account['password'])
            try:
                client.login()
                eligibility = client.can_search()
                if not eligibility.get('can_search'):
                    continue
                result = client.search_authenticated(image_path)
                if result is not None:
                    return {**result, 'account_index': index,
                            'points_balance': eligibility.get('points_balance')}
            except Exception as exc:
                errors.append(f'{type(exc).__name__}: {exc}')
            finally:
                client.close()
        if errors:
            raise RuntimeError('Whos.tv account pool failed: ' + '; '.join(errors[-3:]))
        return None


def _primary_invite_code(username: str, password: str) -> str:
    if not username or not password:
        return ''
    client = WhosTvClient(username, password)
    try:
        client.login()
        return str(client.profile().get('invite_code') or '')
    finally:
        client.close()


def main() -> int:
    load_dotenv(Path(__file__).with_name('.env'))
    parser = argparse.ArgumentParser(description='Register authorized Whos.tv accounts')
    parser.add_argument('--register', type=int, default=1, metavar='COUNT')
    parser.add_argument('--prefix', default='sb')
    args = parser.parse_args()
    if not 1 <= args.register <= 20:
        parser.error('--register must be between 1 and 20')
    store = AccountStore(os.environ.get('WHOS_TV_ACCOUNTS_FILE', DEFAULT_ACCOUNTS_FILE))
    invite = os.environ.get('WHOS_TV_INVITE_CODE', '').strip() or _primary_invite_code(
        os.environ.get('WHOS_TV_USERNAME', '').strip(),
        os.environ.get('WHOS_TV_PASSWORD', '').strip(),
    )
    created = 0
    for _ in range(args.register):
        username, password = generate_credentials(args.prefix)
        session = cffi_requests.Session(impersonate='chrome')
        try:
            register_account(session, username, password, invite_code=invite)
        finally:
            session.close()
        if store.add({'username': username, 'password': password}):
            created += 1
    print(f'REGISTER_OK created={created} pool_total={len(store.load())}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
