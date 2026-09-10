#!/usr/bin/env python3
import contextlib
import io
import os
import tempfile
import unittest
from datetime import date
from unittest import mock

import whos_daily_register


class FakeStore:
    def __init__(self):
        self.accounts = []

    def add(self, account):
        if any(a['username'] == account['username'] for a in self.accounts):
            return False
        self.accounts.append(account)
        return True

    def load(self):
        return list(self.accounts)


def _session_factory():
    session = mock.Mock()
    session.close = mock.Mock()
    return session


class WhosDailyRegisterTests(unittest.TestCase):
    def test_registers_requested_count_and_appends_to_pool(self):
        store = FakeStore()
        registered = []

        def register(session, username, password, *, invite_code=''):
            registered.append(username)
            return {'points_balance': 60}

        result = whos_daily_register.register_daily(
            5, store, session_factory=_session_factory,
            register_account_fn=register, invite_code='INV')

        self.assertEqual(result['created'], 5)
        self.assertEqual(len(store.load()), 5)
        self.assertEqual(result['failed'], 0)
        self.assertEqual(result['pool_total'], 5)

    def test_transient_failure_is_retried_within_budget(self):
        store = FakeStore()
        calls = {'n': 0}

        def register(session, username, password, *, invite_code=''):
            calls['n'] += 1
            if calls['n'] == 1:
                raise RuntimeError('rate limited')
            return {'points_balance': 60}

        result = whos_daily_register.register_daily(
            2, store, session_factory=_session_factory, register_account_fn=register)

        self.assertEqual(result['created'], 2)
        self.assertEqual(result['failed'], 1)
        self.assertEqual(len(result['errors']), 1)
        self.assertIn('rate limited', result['errors'][0])

    def test_partial_result_when_attempt_budget_exhausted(self):
        store = FakeStore()

        def register(session, username, password, *, invite_code=''):
            raise RuntimeError('blocked')

        result = whos_daily_register.register_daily(
            3, store, session_factory=_session_factory, register_account_fn=register)

        self.assertEqual(result['created'], 0)
        self.assertEqual(result['failed'], 3 * 3)
        self.assertEqual(result['pool_total'], 0)

    def test_duplicate_username_is_not_counted_twice(self):
        store = FakeStore()

        def register(session, username, password, *, invite_code=''):
            return {'points_balance': 60}

        with mock.patch.object(whos_daily_register, 'generate_credentials',
                               side_effect=[('dup', 'p'), ('dup', 'p'), ('new', 'p'), ('new2', 'p')]):
            result = whos_daily_register.register_daily(
                2, store, session_factory=_session_factory, register_account_fn=register)

        self.assertEqual(result['created'], 2)
        self.assertEqual([a['username'] for a in store.load()], ['dup', 'new'])

    def test_state_guard_marks_and_detects_same_day(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'state.json')
            self.assertFalse(whos_daily_register.already_registered_today(path))
            whos_daily_register.record_registration(path, 5)
            self.assertTrue(whos_daily_register.already_registered_today(path))
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_state_guard_rejects_stale_date(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'state.json')
            whos_daily_register.record_registration(path, 5, today=date(2020, 1, 1))
            self.assertFalse(whos_daily_register.already_registered_today(path))

    def test_main_skips_when_today_already_done(self):
        with tempfile.TemporaryDirectory() as td:
            state = os.path.join(td, 'state.json')
            accounts = os.path.join(td, 'accounts.json')
            whos_daily_register.record_registration(state, 5)
            env = {
                'WHOS_TV_ACCOUNTS_FILE': accounts,
                'WHOS_TV_REGISTER_STATE_FILE': state,
                'WHOS_TV_DAILY_REGISTER': '5',
            }
            with mock.patch.dict(os.environ, env, clear=False), \
                 mock.patch.object(whos_daily_register, 'register_daily') as daily, \
                 contextlib.redirect_stdout(io.StringIO()):
                code = whos_daily_register.main([])

            self.assertEqual(code, 0)
            daily.assert_not_called()

    def test_main_returns_nonzero_on_partial_registration(self):
        with tempfile.TemporaryDirectory() as td:
            env = {
                'WHOS_TV_ACCOUNTS_FILE': os.path.join(td, 'accounts.json'),
                'WHOS_TV_REGISTER_STATE_FILE': os.path.join(td, 'state.json'),
                'WHOS_TV_DAILY_REGISTER': '5',
                'WHOS_TV_INVITE_CODE': 'INV',
            }
            with mock.patch.dict(os.environ, env, clear=False), \
                 mock.patch.object(whos_daily_register, 'register_daily',
                                   return_value={'created': 3, 'failed': 6, 'pool_total': 3,
                                                 'errors': [], 'accounts': []}) as daily, \
                 contextlib.redirect_stdout(io.StringIO()):
                code = whos_daily_register.main([])

            self.assertEqual(code, 1)
            self.assertEqual(daily.call_args.args[0], 5)


if __name__ == '__main__':
    unittest.main()
