#!/usr/bin/env python3
import hashlib
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

from downloader import (UnsupportedDownloadHost, download_file, download_game_url,
                        parse_mediafire_download, resolve_download_url)


class TinyHandler(BaseHTTPRequestHandler):
    body = b'paid-download-test' * 1024
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Length', str(len(self.body)))
        self.send_header('Content-Disposition', 'attachment; filename="game.zip"')
        self.end_headers()
        self.wfile.write(self.body)
    def log_message(self, *args):
        pass


class DownloaderTests(unittest.TestCase):
    def test_mediafire_page_extracts_real_download_button(self):
        html = '''<html><a id="downloadButton"
          href="https://download1653.mediafire.com/abc/game.rar">DOWNLOAD</a></html>'''
        self.assertEqual(parse_mediafire_download(html),
                         'https://download1653.mediafire.com/abc/game.rar')

    def test_pixeldrain_page_becomes_api_download(self):
        result = resolve_download_url('https://pixeldrain.com/u/EQp1xzVX')
        self.assertEqual(result, 'https://pixeldrain.com/api/file/EQp1xzVX?download')

    def test_unsupported_mega_is_rejected_before_purchase_worker(self):
        with self.assertRaises(UnsupportedDownloadHost):
            resolve_download_url('https://mega.nz/file/abc#key')

    def test_game_download_resolves_share_url_before_streaming(self):
        expected = {'path':'/tmp/game.zip','file_size':1,'checksum':'sha256:a'}
        with patch('downloader.resolve_download_url', return_value='https://pixeldrain.com/api/file/X?download') as resolve, \
             patch('downloader.download_file', return_value=expected) as stream:
            result = download_game_url('https://pixeldrain.com/u/X', '/tmp/out')
        resolve.assert_called_once_with('https://pixeldrain.com/u/X')
        stream.assert_called_once_with('https://pixeldrain.com/api/file/X?download', '/tmp/out', progress=None)
        self.assertEqual(result, expected)

    def test_redirect_target_is_validated_before_second_request(self):
        class Redirect:
            status_code=302
            headers={'Location':'http://127.0.0.1/private'}
            url='https://pixeldrain.com/api/file/X'
            def close(self): pass
        with patch('downloader._validate_public_url', side_effect=UnsupportedDownloadHost('private')) as validate, \
             patch('downloader._one_pinned_get', return_value=Redirect()) as get:
            with self.assertRaises(UnsupportedDownloadHost):
                download_file('https://pixeldrain.com/api/file/X', '/tmp')
        self.assertEqual(get.call_count, 1)
        self.assertEqual(validate.call_count, 1)

    def test_stream_download_is_atomic_and_returns_checksum(self):
        server = HTTPServer(('127.0.0.1', 0), TinyHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                url = f'http://127.0.0.1:{server.server_port}/game.zip'
                result = download_file(url, tmp, allow_private_for_test=True,
                                       max_bytes=10_000_000, reserve_bytes=0)
                path = Path(result['path'])
                self.assertTrue(path.exists())
                self.assertFalse(Path(str(path) + '.part').exists())
                self.assertEqual(result['file_size'], len(TinyHandler.body))
                self.assertEqual(result['checksum'],
                                 'sha256:' + hashlib.sha256(TinyHandler.body).hexdigest())
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
