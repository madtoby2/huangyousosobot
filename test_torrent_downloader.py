#!/usr/bin/env python3
import os
import tempfile
import unittest
import base64
import hashlib
from pathlib import Path

import torrent_downloader
from torrent_downloader import (InvalidMagnet, TorrentTooLarge, TorrentDownloadError, download_magnet,
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


class VideoTorrentDownloaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()
    @staticmethod
    def _bencode(value):
        if isinstance(value, int):
            return b'i' + str(value).encode() + b'e'
        if isinstance(value, bytes):
            return str(len(value)).encode() + b':' + value
        if isinstance(value, str):
            return VideoTorrentDownloaderTests._bencode(value.encode())
        if isinstance(value, list):
            return b'l' + b''.join(VideoTorrentDownloaderTests._bencode(x) for x in value) + b'e'
        if isinstance(value, dict):
            return b'd' + b''.join(
                VideoTorrentDownloaderTests._bencode(k) + VideoTorrentDownloaderTests._bencode(value[k])
                for k in sorted(value)) + b'e'
        raise TypeError(type(value))

    def _torrent_fixture(self, files):
        info = {b'name': b'Bundle', b'piece length': 16384, b'pieces': b'', b'files': files}
        info_bytes = self._bencode(info)
        torrent_bytes = self._bencode({b'info': info})
        info_hash = hashlib.sha1(info_bytes).hexdigest()
        return f'magnet:?xt=urn:btih:{info_hash}&dn=test', torrent_bytes

    def _fake_aria2_video(self, torrent_bytes, state_path, selected_index, payload=b'video-payload'):
        encoded = base64.b64encode(torrent_bytes).decode()
        script = (
            '#!/usr/bin/env python3\nimport base64,sys\nfrom pathlib import Path\n'
            'args=sys.argv[1:]\n'
            "directory=Path(next(a.split('=',1)[1] for a in args if a.startswith('--dir=')))\n"
            "if '--bt-metadata-only=true' in args:\n"
            f" (directory/'metadata.torrent').write_bytes(base64.b64decode('{encoded}'))\n"
            'else:\n'
            f" Path({str(state_path)!r}).write_text(next(a.split('=',1)[1] for a in args if a.startswith('--select-file=')))\n"
            f" out=directory/'Bundle'/'main.mkv'; out.parent.mkdir(parents=True,exist_ok=True); out.write_bytes({payload!r})\n"
        )
        path=self.root/'fake-video-aria2.py'; path.write_text(script); path.chmod(0o755); return str(path)

    def test_downloads_only_one_selected_video_without_zipping_torrent_bundle(self):
        files=[
            {b'length':2048,b'path':[b'readme.txt']},
            {b'length':5,b'path':[b'clip.mp4']},
            {b'length':13,b'path':[b'main.mkv']},
        ]
        magnet, torrent_bytes=self._torrent_fixture(files)
        state=self.root/'selected-index.txt'
        fake=self._fake_aria2_video(torrent_bytes,state,3)
        download=getattr(torrent_downloader,'download_video_magnet',None)
        self.assertTrue(callable(download),'download_video_magnet must select one video file')
        result=download(magnet,str(self.root/'job'),'Daily Video',aria2_path=fake,
                        max_bytes=1024,reserve_bytes=0,timeout=10)
        output=Path(result['path'])
        self.assertEqual(output.name,'Daily Video.mkv')
        self.assertEqual(output.read_bytes(),b'video-payload')
        self.assertEqual(state.read_text(),'3')
        self.assertFalse(output.suffix=='.zip')

    def test_skips_torrents_without_video_files(self):
        magnet,torrent_bytes=self._torrent_fixture([
            {b'length':10,b'path':[b'readme.txt']},
            {b'length':20,b'path':[b'archive.zip']},
        ])
        fake=self._fake_aria2_video(torrent_bytes,self.root/'selected.txt',1)
        download=getattr(torrent_downloader,'download_video_magnet',None)
        self.assertTrue(callable(download),'download_video_magnet must be implemented')
        with self.assertRaises(TorrentDownloadError):
            download(magnet,str(self.root/'job'),'No Video',aria2_path=fake,
                     max_bytes=1024,reserve_bytes=0,timeout=10)
        self.assertFalse((self.root/'selected.txt').exists())

    def test_skips_when_every_video_exceeds_size_limit(self):
        magnet,torrent_bytes=self._torrent_fixture([
            {b'length':2048,b'path':[b'main.mp4']},
        ])
        fake=self._fake_aria2_video(torrent_bytes,self.root/'selected.txt',1)
        download=getattr(torrent_downloader,'download_video_magnet',None)
        self.assertTrue(callable(download),'download_video_magnet must be implemented')
        with self.assertRaises(TorrentTooLarge):
            download(magnet,str(self.root/'job'),'Too Large',aria2_path=fake,
                     max_bytes=1024,reserve_bytes=0,timeout=10)
        self.assertFalse((self.root/'selected.txt').exists())


if __name__ == '__main__':
    unittest.main()
