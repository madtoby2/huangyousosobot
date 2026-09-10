#!/usr/bin/env python3
import unittest
from unittest import mock

import bot


class BotWhosPoolTests(unittest.TestCase):
    def test_run_whos_search_uses_configured_account_pool(self):
        fake = mock.Mock()
        fake.search.return_value = {'matches': [{'code': 'ABC-123'}]}
        accounts = [{'username': 'one111', 'password': 'pass111'}]
        with mock.patch.object(bot.whos_accounts, 'load_accounts', return_value=accounts), \
             mock.patch.object(bot.whos_accounts, 'WhosAccountPool', return_value=fake):
            result = bot._run_whos_search('/tmp/frame.jpg')
        fake.search.assert_called_once_with('/tmp/frame.jpg')
        self.assertEqual(result['matches'][0]['code'], 'ABC-123')

    def test_start_copy_mentions_whos_pool_and_fallback(self):
        text = bot._start_text()
        self.assertIn('Whos.tv', text)
        self.assertIn('Yandex', text)


if __name__ == '__main__':
    unittest.main()
