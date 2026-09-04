#!/usr/bin/env python3
import os
import tempfile
import unittest
from pathlib import Path

from torrent_downloader import (InvalidMagnet, TorrentTooLarge, download_magnet,
                                magnet_info_hash)

HASH = '47a51b8012cd969076ae0a3ae7c65465411a4e0c'
MAGNET = f'magnet:?xt=urn:btih:{HASH}&dn=source&tr=udp%3A%2F%2Ftracker.example'


class TorrentDownloaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _fake_aria2(self, body: str):
        path = self.root / 'fake-aria2.py'
        path.write_text('#!/usr/bin/env python3\nimport pathlib,sys\n' + body)
        path.chmod(0o755)
        return str(path)

    def test_validates_and_extracts_btih(self):
        self.assertEqual(magnet_info_hash(MAGNET), HASH)
        for bad in ('', 'https://example.com/x', 'magnet:?dn=x',
                    'magnet:?xt=urn:btih:../../etc/passwd'):
            with self.subTest(bad=bad), self.assertRaises(InvalidMagnet):
                magnet_info_hash(bad)

    def test_downloads_single_file_and_renames_to_clean_title(self):
        fake = self._fake_aria2(
            "d=next(x.split('=',1)[1] for x in sys.argv if x.startswith('--dir=')); "
            "pathlib.Path(d,'source-video.mp4').write_bytes(b'video-data')\n"
        )
        result = download_magnet(MAGNET, str(self.root / 'job'),
                                 '[Sukebei] NHDTB-706 decrypted',
                                 aria2_path=fake, reserve_bytes=0, timeout=10)
        output = Path(result['path'])
        self.assertEqual(output.name, 'NHDTB-706.mp4')
        self.assertEqual(output.read_bytes(), b'video-data')
        self.assertEqual(result['file_size'], 10)
        self.assertTrue(result['checksum'].startswith('sha256:'))

    def test_kills_download_when_payload_exceeds_limit(self):
        fake = self._fake_aria2(
            "d=next(x.split('=',1)[1] for x in sys.argv if x.startswith('--dir=')); "
            "pathlib.Path(d,'huge.bin').write_bytes(b'x'*2048)\n"
        )
        with self.assertRaises(TorrentTooLarge):
            download_magnet(MAGNET, str(self.root / 'job'), 'Test',
                            aria2_path=fake, max_bytes=1024,
                            reserve_bytes=0, timeout=10)


if __name__ == '__main__':
    unittest.main()
