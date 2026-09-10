#!/usr/bin/env python3
"""Daily top-up of fresh Whos.tv accounts into the private pool."""
from __future__ import annotations

import json
import os
import secrets
import sys
from datetime import date
from pathlib import Path

from curl_cffi import requests as cffi_requests
from dotenv import load_dotenv

from whos_accounts import (AccountStore, DEFAULT_ACCOUNTS_FILE, generate_credentials,
                           register_account, resolve_invite_code)

DEFAULT_DAILY_COUNT = 5
DEFAULT_STATE_FILE = str(Path(__file__).with_name('whos_register_state.json'))


def _read_state(state_path: str) -> dict:
    path = Path(state_path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def already_registered_today(state_path: str, today: date | None = None) -> bool:
    """True when the daily registration already ran on the given UTC date."""
    stamp = (today or date.today()).isoformat()
    return str(_read_state(state_path).get('date') or '') == stamp


def record_registration(state_path: str, created: int, today: date | None = None) -> None:
    """Persist the registration marker atomically (0600, owner-only)."""
    path = Path(state_path)
    payload = {'date': (today or date.today()).isoformat(), 'created': int(created)}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'.{path.name}.{secrets.token_hex(4)}.tmp')
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        if tmp.exists():
            tmp.unlink()


def register_daily(count: int, store, *, session_factory, register_account_fn,
                   invite_code: str = '', prefix: str = 'sb',
                   attempts_per_account: int = 3) -> dict:
    """Register up to ``count`` accounts, retrying transient failures.

    A single failed registration never aborts the batch: each account gets
    ``attempts_per_account`` tries, and the batch stops once ``count`` accounts
    were created or the attempt budget runs out.
    """
    created = 0
    failed = 0
    errors: list[str] = []
    accounts: list[dict] = []
    budget = max(count, 1) * max(attempts_per_account, 1)
    attempts = 0
    while created < count and attempts < budget:
        attempts += 1
        username, password = generate_credentials(prefix)
        session = session_factory()
        try:
            register_account_fn(session, username, password, invite_code=invite_code)
        except Exception as exc:  # noqa: BLE001 - per-account failure must not abort the batch
            failed += 1
            errors.append(f'{type(exc).__name__}: {exc}')
            continue
        finally:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass
        if store.add({'username': username, 'password': password}):
            created += 1
            accounts.append({'username': username, 'password': password})
    return {'created': created, 'failed': failed, 'errors': errors,
            'accounts': accounts, 'pool_total': len(store.load())}


def main(argv: list[str] | None = None) -> int:
    load_dotenv(Path(__file__).with_name('.env'))
    args = list(sys.argv[1:] if argv is None else argv)
    force = '--force' in args
    try:
        count = int(os.environ.get('WHOS_TV_DAILY_REGISTER', '') or DEFAULT_DAILY_COUNT)
    except ValueError:
        count = DEFAULT_DAILY_COUNT
    count = max(0, min(count, 20))
    prefix = os.environ.get('WHOS_TV_REGISTER_PREFIX', 'sb') or 'sb'
    store = AccountStore(os.environ.get('WHOS_TV_ACCOUNTS_FILE', DEFAULT_ACCOUNTS_FILE))
    state_path = os.environ.get('WHOS_TV_REGISTER_STATE_FILE', DEFAULT_STATE_FILE)

    if not force and already_registered_today(state_path):
        print(f'REGISTER_SKIP already_registered_today pool_total={len(store.load())}')
        return 0
    if count == 0:
        print(f'REGISTER_SKIP disabled pool_total={len(store.load())}')
        return 0

    invite = os.environ.get('WHOS_TV_INVITE_CODE', '').strip() or resolve_invite_code(
        os.environ.get('WHOS_TV_USERNAME', '').strip(),
        os.environ.get('WHOS_TV_PASSWORD', '').strip(),
    )
    result = register_daily(
        count, store,
        session_factory=lambda: cffi_requests.Session(impersonate='chrome'),
        register_account_fn=register_account,
        invite_code=invite, prefix=prefix,
    )
    record_registration(state_path, result['created'])
    for error in result['errors'][:5]:
        print(f'REGISTER_ERROR {error}')
    print(f"REGISTER_OK created={result['created']} failed={result['failed']} "
          f"pool_total={result['pool_total']}")
    return 0 if result['created'] == count else 1


if __name__ == '__main__':
    raise SystemExit(main())
