import os
import tempfile
import unittest

from wallet_store import WalletStore


class SearchHistoryTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix='.sqlite3')
        os.close(handle)
        self.store = WalletStore(self.path)

    def tearDown(self):
        os.unlink(self.path)

    def test_search_history_is_private_and_newest_first(self):
        self.store.record_search(10, 'ryu', 'alpha', 'Alpha Game', 'ryu:alpha')
        self.store.record_search(10, 'bt', 'beta', 'Beta Movie', 'bt:beta')
        self.store.record_search(20, 'ryu', 'other', 'Other', 'ryu:other')
        rows = self.store.search_history_for(10)
        self.assertEqual([x['query'] for x in rows], ['beta', 'alpha'])
        self.assertEqual([x['title'] for x in rows], ['Beta Movie', 'Alpha Game'])


if __name__ == '__main__':
    unittest.main()
