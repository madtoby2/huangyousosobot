#!/usr/bin/env python3
"""BT image-search-to-delivery integration over an isolated real aria2 peer.

No public tracker, Whos account, payment provider, Telegram API, or production
wallet/channel is used. Only the BitTorrent peer protocol runs for real.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote_to_bytes
from unittest import mock

import bot
from pipeline import process_download_job
from torrent_downloader import download_magnet
from wallet_store import WalletStore


def _bencode(value):
    if isinstance(value, int):
        return b'i' + str(value).encode('ascii') + b'e'
    if isinstance(value, bytes):
        return str(len(value)).encode('ascii') + b':' + value
    if isinstance(value, list):
        return b'l' + b''.join(_bencode(item) for item in value) + b'e'
    if isinstance(value, dict):
        return b'd' + b''.join(
            _bencode(key) + _bencode(value[key]) for key in sorted(value)
        ) + b'e'
    raise TypeError(f'unsupported bencode value: {type(value).__name__}')


def _free_tcp_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class _LocalTracker:
    """Tiny compact-peer HTTP tracker for the local acceptance test."""

    def __init__(self):
        self._lock = threading.Lock()
        self._peers: dict[bytes, dict[bytes, tuple[int, bytes]]] = {}
        self._announces = []
        tracker = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                raw_query = self.path.partition('?')[2].encode('ascii', errors='ignore')
                values = {}
                for field in raw_query.split(b'&'):
                    key, sep, value = field.partition(b'=')
                    if sep:
                        values[unquote_to_bytes(key).decode('ascii', errors='ignore')] = unquote_to_bytes(value)
                info_hash = values.get('info_hash', b'')
                peer_id = values.get('peer_id', b'')
                try:
                    port = int(values.get('port', b'0'))
                except ValueError:
                    port = 0
                event = values.get('event', b'')
                left = values.get('left', b'')
                compact = bytearray()
                with tracker._lock:
                    if event == b'stopped':
                        tracker._peers.get(info_hash, {}).pop(peer_id, None)
                    elif info_hash and peer_id and 0 < port < 65536:
                        tracker._peers.setdefault(info_hash, {})[peer_id] = (port, left)
                    for other_id, (other_port, other_left) in tracker._peers.get(info_hash, {}).items():
                        if other_id != peer_id and other_left == b'0':
                            compact.extend(b'\x7f\x00\x00\x01')
                            compact.extend(other_port.to_bytes(2, 'big'))
                body = b'd8:intervali1e5:peers' + str(len(compact)).encode('ascii') + b':' + bytes(compact) + b'e'
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}/announce'

    def wait_for_seed(self, info_hash: bytes, process, timeout: float = 12.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if self._peers.get(info_hash):
                    return
            if process.poll() is not None:
                raise AssertionError(f'aria2 seeder exited early with code {process.returncode}')
            time.sleep(0.05)
        raise AssertionError(f'aria2 seeder did not announce as complete; announces={self._announces!r}')

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


class _FakeUploader:
    def __init__(self, expected_bytes: bytes):
        self.expected_bytes = expected_bytes
        self.calls = []

    async def upload(self, path, caption):
        artifact = Path(path)
        payload = artifact.read_bytes()
        assert payload == self.expected_bytes, 'uploaded bytes differ from the local seed'
        self.calls.append({'path': str(artifact), 'name': artifact.name,
                           'caption': caption, 'sha256': hashlib.sha256(payload).hexdigest()})
        return {'storage_chat_id': -1009876543210, 'storage_message_id': 314}


class _FakeTelegramBot:
    def __init__(self):
        self.calls = []

    async def copy_message(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(message_id=2718)


@unittest.skipUnless(shutil.which('aria2c'), 'aria2c is required for the local-peer acceptance test')
class BtPurchaseLocalPeerE2ETests(unittest.IsolatedAsyncioTestCase):
    async def test_photo_match_to_paid_bt_download_storage_and_buyer_delivery(self):
        asyncio.get_running_loop().slow_callback_duration = 1.0
        with tempfile.TemporaryDirectory(prefix='bt-e2e-') as tmp:
            root = Path(tmp)
            tracker = _LocalTracker()
            seed_dir = root / 'seed'
            seed_dir.mkdir()
            seeded_bytes = bytes((i * 31 + 7) % 256 for i in range(384 * 1024 + 173))
            (seed_dir / 'seed-source.mp4').write_bytes(seeded_bytes)
            piece_length = 32 * 1024
            pieces = b''.join(
                hashlib.sha1(seeded_bytes[offset:offset + piece_length]).digest()
                for offset in range(0, len(seeded_bytes), piece_length)
            )
            info = {
                b'length': len(seeded_bytes),
                b'name': b'seed-source.mp4',
                b'piece length': piece_length,
                b'pieces': pieces,
            }
            info_hash = hashlib.sha1(_bencode(info)).digest()
            torrent_path = root / 'fixture.torrent'
            torrent_path.write_bytes(_bencode({b'announce': tracker.url.encode(), b'info': info}))
            seed_port = _free_tcp_port()
            seed_log = (root / 'seeder.log').open('wb')
            seeder = subprocess.Popen([
                shutil.which('aria2c'), '--enable-dht=false',
                f'--listen-port={seed_port}', '--seed-time=600',
                '--peer-id-prefix=SEED0001',
                '--bt-tracker-connect-timeout=5', '--bt-tracker-timeout=5',
                '--file-allocation=none', '--allow-overwrite=true',
                '--check-integrity=true', '--bt-hash-check-seed=true',
                '--summary-interval=0',
                '--console-log-level=warn', '--download-result=hide',
                f'--dir={seed_dir}', str(torrent_path),
            ], stdout=seed_log, stderr=subprocess.STDOUT)
            try:
                try:
                    await asyncio.to_thread(tracker.wait_for_seed, info_hash, seeder)
                except AssertionError as exc:
                    seed_log.flush()
                    details = (root / 'seeder.log').read_text(errors='replace')
                    raise AssertionError(f'{exc}; aria2 log: {details}') from exc

                # The production downloader receives a local wrapper solely to
                # disable all non-local peer discovery; aria2 itself is real.
                aria2_wrapper = root / 'aria2-local-only'
                downloader_log = root / 'downloader.log'
                aria2_wrapper.write_text(
                    '#!/bin/sh\nexec /usr/bin/aria2c '
                    f'--enable-dht=false --bt-tracker={tracker.url} --peer-id-prefix=DOWN0001 '
                    f'--bt-tracker-connect-timeout=5 --bt-tracker-timeout=5 '
                    f'--log={downloader_log} --log-level=debug "$@"\n',
                    encoding='utf-8',
                )
                aria2_wrapper.chmod(0o700)
                magnet = (f'magnet:?xt=urn:btih:{info_hash.hex()}&dn=GANA-2823'
                          f'&tr={tracker.url.replace(":", "%3A").replace("/", "%2F")}')
                search_result = {
                    'code': 'GANA-2823', 'title': 'GANA-2823 sample',
                    'source': 'sukebei', 'source_label': 'Sukebei',
                    'url': 'https://sukebei.nyaa.si/view/local-fixture',
                    'magnet': magnet,
                }

                # Exercise the real image handler and state transition; only
                # the upstream recognition and catalogue services are mocked.
                user_id = 912345671
                bot._state(user_id).clear()
                status = SimpleNamespace(edit_text=mock.AsyncMock())
                message = SimpleNamespace(
                    photo=[SimpleNamespace(file_id='local-photo-fixture')],
                    document=None,
                    reply_text=mock.AsyncMock(return_value=status),
                )
                update = SimpleNamespace(effective_user=SimpleNamespace(id=user_id), message=message)
                image_file = SimpleNamespace(download_to_drive=mock.AsyncMock())
                context = SimpleNamespace(bot=SimpleNamespace(
                    get_file=mock.AsyncMock(return_value=image_file)))
                with (mock.patch.object(bot, '_run_whos_search', return_value={
                          'matches': [{'code': 'GANA-2823', 'similarity': 98.2}]}),
                      mock.patch.object(bot, '_run_yandex_search', return_value=None),
                      mock.patch.object(bot.search_bt, 'search', return_value={'results': [search_result]}),
                      mock.patch.object(bot, '_render_page', new=mock.AsyncMock()) as render):
                    await bot.handle_photo(update, context)
                render.assert_awaited_once()
                search_state = bot._state(user_id)
                self.assertEqual(search_state['domain'], 'bt')
                self.assertEqual(search_state['keyword'], 'GANA-2823')
                self.assertEqual(search_state['results'][0]['magnet'], magnet)

                offer = bot._select_bt_offer(search_state['results'][0], 10_000_000)
                self.assertIsNotNone(offer)
                db_path = root / 'isolated-wallet.sqlite3'
                store = WalletStore(str(db_path))
                topup = store.create_topup(user_id, '1', 'USDT')
                provider_order_id = 'local-e2e-' + topup['order_id']
                store.attach_provider(topup['order_id'], provider_order_id,
                                      'https://pay.example/local-e2e')
                self.assertTrue(store.credit_verified({
                    'order_id': topup['order_id'], 'provider_order_id': provider_order_id,
                    'coin': 'USDT', 'amount': '1',
                }))
                purchase, charged, job_created = store.create_download_purchase(user_id, offer)
                self.assertTrue(charged)
                self.assertTrue(job_created)
                job = store.job_for_resource(offer['resource_id'])
                self.assertEqual(job['status'], 'queued')

                def local_peer_download(url, destination_dir, title, *, progress=None):
                    return download_magnet(
                        url, destination_dir, title, progress=progress,
                        max_bytes=2 * 1024 * 1024, reserve_bytes=0, timeout=35,
                        aria2_path=str(aria2_wrapper),
                    )

                uploader = _FakeUploader(seeded_bytes)
                buyer_bot = _FakeTelegramBot()
                work_dir = root / 'jobs'
                result = await process_download_job(
                    store, job['job_id'],
                    download_callable=lambda *_args, **_kwargs: self.fail('BT routed to HTTP'),
                    uploader=uploader, bot=buyer_bot, work_dir=str(work_dir),
                    torrent_callable=local_peer_download,
                )

                if result['status'] != 'ready':
                    log_text = downloader_log.read_text(errors='replace') if downloader_log.exists() else '<no aria2 log>'
                    self.fail(f"download failed: {result}; aria2 log: {log_text[-6000:]}")
                self.assertEqual(result['delivered'], 1)
                self.assertEqual(result['refunded'], 0)
                self.assertEqual(len(uploader.calls), 1)
                self.assertEqual(uploader.calls[0]['name'], 'GANA-2823.mp4')
                self.assertEqual(uploader.calls[0]['caption'], 'GANA-2823')
                self.assertEqual(uploader.calls[0]['sha256'], hashlib.sha256(seeded_bytes).hexdigest())
                self.assertEqual(buyer_bot.calls, [{
                    'chat_id': user_id, 'from_chat_id': -1009876543210,
                    'message_id': 314,
                }])
                self.assertEqual(store.get_purchase(purchase['purchase_id'])['status'], 'delivered')
                self.assertEqual(store.get_balance_units(user_id), 90_000_000)
                self.assertEqual(store.get_resource(offer['resource_id'])['cache_status'], 'ready')
                self.assertEqual(list(work_dir.iterdir()), [])
                self.assertEqual(store.ledger_for(user_id)[-1]['kind'], 'purchase')
            finally:
                if seeder.poll() is None:
                    seeder.terminate()
                    try:
                        seeder.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        seeder.kill()
                        seeder.wait(timeout=5)
                seed_log.close()
                tracker.close()
                bot._state(912345671).clear()


if __name__ == '__main__':
    unittest.main()
