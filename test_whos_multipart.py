#!/usr/bin/env python3
import tempfile
import unittest
from unittest import mock

import whos_tv


class WhosMultipartUploadTests(unittest.TestCase):
    def test_upload_uses_curl_multipart_form(self):
        session = mock.Mock()
        response = mock.Mock(text='https://whos.tv/search-wait/abc')
        response.raise_for_status = mock.Mock()
        session.post.return_value = response
        client = whos_tv.WhosTvClient(
            'user111', 'pass111', session_factory=mock.Mock(return_value=session))
        with tempfile.NamedTemporaryFile(suffix='.jpg') as image:
            image.write(b'jpeg-data'); image.flush()
            result = client._upload(image.name)
        self.assertIn('multipart', session.post.call_args.kwargs)
        self.assertNotIn('files', session.post.call_args.kwargs)
        self.assertEqual(result, 'https://whos.tv/search-wait/abc')


if __name__ == '__main__':
    unittest.main()
