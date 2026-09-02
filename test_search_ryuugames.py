#!/usr/bin/env python3
import json
import unittest
from unittest.mock import patch

import search_ryuugames


class FakeResponse:
    status_code = 200

    def json(self):
        return {
            'success': True,
            '0': {
                'destination': 'https://anchoreth.com/r-adsh?t=i&v=ignored',
                'metadata': json.dumps({
                    'url': 'https://www.mediafire.com/file/test/game.rar/file'
                }),
            },
        }


class FakeSession:
    def __init__(self, failures=0):
        self.failures = failures
        self.calls = 0

    def post(self, *args, **kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            raise TimeoutError('temporary timeout')
        return FakeResponse()


class AlwaysFailSession:
    def post(self, *args, **kwargs):
        raise TimeoutError('down')


class RyuugamesDownloadTests(unittest.TestCase):
    def test_adshrink_shortlink_resolves_to_download_host(self):
        with patch.object(search_ryuugames, '_sess', return_value=FakeSession()):
            actual = search_ryuugames._resolve_adshrink_url('https://ashnk.com/EA6vsv')
        self.assertEqual(actual, 'https://www.mediafire.com/file/test/game.rar/file')

    def test_adshrink_resolution_retries_transient_failure(self):
        session = FakeSession(failures=1)
        with patch.object(search_ryuugames, '_sess', return_value=session):
            actual = search_ryuugames._resolve_adshrink_url('https://ashnk.com/EA6vsv')
        self.assertEqual(actual, 'https://www.mediafire.com/file/test/game.rar/file')
        self.assertEqual(session.calls, 2)

    def test_unresolved_adshrink_link_is_dropped(self):
        with patch.object(search_ryuugames, '_sess', return_value=AlwaysFailSession()):
            actual = search_ryuugames._resolve_adshrink_url('https://ashnk.com/EA6vsv')
        self.assertEqual(actual, '')

    def test_non_adshrink_link_is_unchanged(self):
        direct = 'https://mega.nz/file/example#key'
        self.assertEqual(search_ryuugames._resolve_adshrink_url(direct), direct)

    def test_only_real_download_hosts_are_accepted(self):
        self.assertTrue(search_ryuugames._is_download_url('https://mega.nz/file/example#key'))
        self.assertTrue(search_ryuugames._is_download_url('https://pixeldrain.com/u/example'))
        self.assertFalse(search_ryuugames._is_download_url('https://ouo.io/example'))
        self.assertFalse(search_ryuugames._is_download_url('https://ashnk.com/example'))


if __name__ == '__main__':
    unittest.main()
