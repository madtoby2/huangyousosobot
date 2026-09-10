#!/usr/bin/env python3
import unittest
from unittest import mock

import whos_tv


class WhosTlsClientTests(unittest.TestCase):
    def test_client_uses_chrome_impersonation_session(self):
        session = mock.Mock()
        factory = mock.Mock(return_value=session)
        client = whos_tv.WhosTvClient('user111', 'pass111', session_factory=factory)
        factory.assert_called_once_with(impersonate='chrome')
        self.assertIs(client.session, session)


if __name__ == '__main__':
    unittest.main()
