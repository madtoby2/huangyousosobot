#!/usr/bin/env python3
import json
import os
import tempfile
import unittest
from unittest import mock

try:
    import whos_accounts
except ImportError:
    whos_accounts = None


class WhosAccountTests(unittest.TestCase):
    def test_module_exists(self):
        self.assertIsNotNone(whos_accounts)

    def test_register_posts_supported_payload_and_verifies_profile(self):
        session = mock.Mock()
        created = mock.Mock(); created.raise_for_status = mock.Mock()
        created.json.return_value = {'code': 200000, 'message': '注册成功'}
        profile = mock.Mock(); profile.raise_for_status = mock.Mock()
        profile.json.return_value = {'code': 200000, 'data': {'id': 9, 'points_balance': 60}}
        session.post.return_value = created
        session.get.return_value = profile

        result = whos_accounts.register_account(
            session, 'member123', 'secret99', invite_code='INVITE88')

        self.assertEqual(result['points_balance'], 60)
        session.post.assert_called_once_with(
            'https://whos.tv/api/register',
            json={'username': 'member123', 'password': 'secret99', 'invite_code': 'INVITE88'},
            timeout=30,
        )
        session.get.assert_called_once_with('https://whos.tv/api/user/profile', timeout=30)

    def test_account_store_is_atomic_private_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'accounts.json')
            store = whos_accounts.AccountStore(path)
            store.add({'username': 'u123456', 'password': 'p123456'})
            store.add({'username': 'u123456', 'password': 'different'})
            self.assertEqual(store.load(), [{'username': 'u123456', 'password': 'p123456'}])
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            with open(path, encoding='utf-8') as fh:
                self.assertEqual(json.load(fh)[0]['username'], 'u123456')

    def test_pool_skips_account_without_points(self):
        accounts = [
            {'username': 'first11', 'password': 'pass111'},
            {'username': 'second2', 'password': 'pass222'},
        ]
        clients = []
        for can, result in [
            ({'can_search': False, 'points_balance': 0}, None),
            ({'can_search': True, 'points_balance': 60}, {'matches': [{'code': 'ABC-123'}]}),
        ]:
            client = mock.Mock()
            client.can_search.return_value = can
            client.search_authenticated.return_value = result
            clients.append(client)
        factory = mock.Mock(side_effect=clients)
        pool = whos_accounts.WhosAccountPool(accounts, client_factory=factory)

        result = pool.search('/tmp/frame.jpg')

        self.assertEqual(result['matches'][0]['code'], 'ABC-123')
        clients[0].search_authenticated.assert_not_called()
        clients[1].search_authenticated.assert_called_once_with('/tmp/frame.jpg')
        self.assertTrue(all(c.close.called for c in clients))


if __name__ == '__main__':
    unittest.main()
