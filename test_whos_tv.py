#!/usr/bin/env python3
import tempfile
import unittest
from unittest import mock

try:
    import whos_tv
except ImportError:
    whos_tv = None


RESULT_HTML = '''
<div class="rounded-xl overflow-hidden bg-white/3">
  <a href="/videos/gana-2823"><p><span class="text-primary">GANA-2823</span> Sample title</p></a>
  <div class="result-image-best-match-frames">
    <a href="/frames/1"><img src="https://img.test/frame.webp"><div>99.2%</div><span>01:00:40</span></a>
  </div>
</div>
'''


class WhosTvTests(unittest.TestCase):
    def test_module_exists(self):
        self.assertIsNotNone(whos_tv)

    def test_parse_result_extracts_code_similarity_and_timestamp(self):
        self.assertIsNotNone(whos_tv)
        result = whos_tv.parse_result_page(RESULT_HTML, 'https://whos.tv/search-img/task')
        self.assertEqual(result['matches'][0]['code'], 'GANA-2823')
        self.assertEqual(result['matches'][0]['similarity'], 99.2)
        self.assertEqual(result['matches'][0]['at'], '01:00:40')

    def test_client_logs_in_uploads_and_polls(self):
        self.assertIsNotNone(whos_tv)
        client = whos_tv.WhosTvClient('user', 'secret', poll_interval=0, max_wait=5)
        login = mock.Mock(status_code=200); login.json.return_value={'code': 200000}; login.raise_for_status=mock.Mock()
        upload = mock.Mock(status_code=200, text='https://whos.tv/search-wait/task123'); upload.raise_for_status=mock.Mock()
        status = mock.Mock(status_code=200, text='https://whos.tv/search-img/task123'); status.raise_for_status=mock.Mock()
        page = mock.Mock(status_code=200, text=RESULT_HTML); page.raise_for_status=mock.Mock()
        client.session.post = mock.Mock(side_effect=[login, upload])
        client.session.get = mock.Mock(side_effect=[status, page])
        with tempfile.NamedTemporaryFile(suffix='.jpg') as image:
            result = client.search(image.name)
        self.assertEqual(result['matches'][0]['code'], 'GANA-2823')
        client.close()


if __name__ == '__main__':
    unittest.main()
