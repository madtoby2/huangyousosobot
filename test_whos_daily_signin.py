#!/usr/bin/env python3
import unittest
from unittest import mock

try:
    import whos_daily_signin
except ImportError:
    whos_daily_signin = None


class WhosDailySigninTests(unittest.TestCase):
    def test_module_exists(self):
        self.assertIsNotNone(whos_daily_signin)

    def test_login_auto_claims_daily_signin_and_returns_balance(self):
        self.assertIsNotNone(whos_daily_signin)
        session = mock.Mock()
        login = mock.Mock(); login.raise_for_status = mock.Mock(); login.json.return_value = {'code': 200000}
        tasks = mock.Mock(); tasks.raise_for_status = mock.Mock(); tasks.json.return_value = {
            'data': {'daily': [{'task_key': 'daily_signin', 'progress': {
                'status': 2, 'claimed_at': '2026-09-10 00:10:00',
                'meta': {'consecutive_days': 3},
            }}]}
        }
        profile = mock.Mock(); profile.raise_for_status = mock.Mock(); profile.json.return_value = {
            'data': {'points_balance': 30}
        }
        session.post.return_value = login
        session.get.side_effect = [tasks, profile]

        result = whos_daily_signin.run_signin(session, 'user', 'secret')

        self.assertEqual(result['points_balance'], 30)
        self.assertEqual(result['consecutive_days'], 3)
        self.assertEqual(result['claimed_at'], '2026-09-10 00:10:00')
        session.post.assert_called_once()

    def test_unclaimed_signin_raises(self):
        self.assertIsNotNone(whos_daily_signin)
        session = mock.Mock()
        login = mock.Mock(); login.raise_for_status = mock.Mock(); login.json.return_value = {'code': 200000}
        tasks = mock.Mock(); tasks.raise_for_status = mock.Mock(); tasks.json.return_value = {
            'data': {'daily': [{'task_key': 'daily_signin', 'progress': {'status': 0}}]}
        }
        session.post.return_value = login
        session.get.return_value = tasks
        with self.assertRaises(RuntimeError):
            whos_daily_signin.run_signin(session, 'user', 'secret')


if __name__ == '__main__': unittest.main()
