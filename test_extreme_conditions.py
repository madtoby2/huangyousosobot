#!/usr/bin/env python3
import asyncio
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from artifacts import game_resource_id
from delivery import DeliveryFailed, deliver_purchase
from downloader import DownloadError, download_file
from pipeline import cleanup_stale_job_dirs, process_download_job
from wallet_store import PaymentMismatch, WalletStore


class BaseStoreTest(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix='.sqlite3'); os.close(fd)
        self.store = WalletStore(self.db)

    def tearDown(self):
        for suffix in ('', '-shm', '-wal'):
            try: os.unlink(self.db + suffix)
            except FileNotFoundError: pass

    def fund(self, user, amount='3'):
        order = self.store.create_topup(user, amount, 'USDT')
        provider = 'p-' + order['order_id']
        self.store.attach_provider(order['order_id'], provider, 'https://pay.example/x')
        self.store.credit_verified({'order_id': order['order_id'], 'provider_order_id': provider,
                                    'coin': 'USDT', 'amount': amount})

    def offer(self, version='1', url='https://pixeldrain.com/u/ONE'):
        source_url = 'https://ryuugames.com/game/'
        return {'resource_id': game_resource_id('ryuugames', source_url, version, url),
                'title': 'Game', 'source': 'ryuugames', 'source_url': source_url,
                'download_url': url, 'version': version, 'price_units': 100000000}


class IdentityAndStateTests(BaseStoreTest):
    def test_version_or_download_change_creates_distinct_resource_identity(self):
        a = game_resource_id('ryuugames', 'https://ryuugames.com/game/', '1',
                             'https://pixeldrain.com/u/ONE')
        b = game_resource_id('ryuugames', 'https://ryuugames.com/game/', '2',
                             'https://pixeldrain.com/u/TWO')
        self.assertNotEqual(a, b)

    def test_same_resource_id_cannot_mutate_download_snapshot(self):
        self.fund(1); first = self.offer()
        self.store.create_download_purchase(1, first)
        self.fund(2); changed = dict(first, version='2', download_url='https://pixeldrain.com/u/TWO')
        with self.assertRaises(PaymentMismatch):
            self.store.create_download_purchase(2, changed)
        self.assertEqual(self.store.get_balance_units(2), 300000000)

    def test_database_rejects_invalid_job_and_purchase_statuses(self):
        self.fund(1); offer=self.offer(); purchase,_,_=self.store.create_download_purchase(1,offer)
        job=self.store.job_for_resource(offer['resource_id'])
        with self.store._connect() as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE download_jobs SET status='nonsense' WHERE job_id=?",(job['job_id'],))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE download_purchases SET status='nonsense' WHERE purchase_id=?",(purchase['purchase_id'],))

    def test_complete_requires_uploading_state(self):
        self.fund(1); offer = self.offer(); self.store.create_download_purchase(1, offer)
        job = self.store.job_for_resource(offer['resource_id'])
        with self.assertRaises(PaymentMismatch):
            self.store.complete_download(job['job_id'], storage_chat_id=-1001,
                                         storage_message_id=2, file_size=1,
                                         checksum='sha256:x')

    def test_restart_moves_ambiguous_delivery_to_manual_review(self):
        self.fund(1); offer = self.offer(); purchase, _, _ = self.store.create_download_purchase(1, offer)
        self.assertTrue(self.store.claim_delivery(purchase['purchase_id']))
        self.assertEqual(self.store.reset_interrupted_deliveries(), 1)
        self.assertEqual(self.store.get_purchase(purchase['purchase_id'])['status'], 'manual_review')

    def test_admin_cannot_refund_delivery_in_flight(self):
        self.fund(1); offer = self.offer(); purchase, _, _ = self.store.create_download_purchase(1, offer)
        self.store.claim_delivery(purchase['purchase_id'])
        with self.assertRaises(PaymentMismatch):
            self.store.refund_purchase(purchase['purchase_id'], 'admin')

    def test_manual_review_can_be_resolved_without_racing_active_delivery(self):
        self.fund(1); offer = self.offer(); purchase, _, _ = self.store.create_download_purchase(1, offer)
        self.store.claim_delivery(purchase['purchase_id'])
        self.store.mark_delivery_unknown(purchase['purchase_id'], 'timeout')
        self.assertTrue(self.store.resolve_manual_delivery(purchase['purchase_id'], 991))
        row = self.store.get_purchase(purchase['purchase_id'])
        self.assertEqual((row['status'], row['telegram_message_id']), ('delivered', 991))

    def test_manual_review_can_be_refunded_once_after_operator_check(self):
        self.fund(1); offer = self.offer(); purchase, _, _ = self.store.create_download_purchase(1, offer)
        self.store.claim_delivery(purchase['purchase_id'])
        self.store.mark_delivery_unknown(purchase['purchase_id'], 'timeout')
        self.assertTrue(self.store.refund_purchase(purchase['purchase_id'], 'operator verified no delivery'))
        self.assertFalse(self.store.refund_purchase(purchase['purchase_id'], 'again'))
        self.assertEqual(self.store.get_balance_units(1), 300000000)

    def test_ready_pending_delivery_scan_survives_restart(self):
        self.fund(1); offer = self.offer(); purchase, _, _ = self.store.create_download_purchase(1, offer)
        job = self.store.job_for_resource(offer['resource_id'])
        self.store.claim_download(job['job_id']); self.store.mark_uploading(job['job_id'], '/tmp/x')
        self.store.complete_download(job['job_id'], storage_chat_id=-1001,
                                     storage_message_id=2, file_size=1, checksum='sha256:x')
        reopened = WalletStore(self.db)
        self.assertEqual([x['purchase_id'] for x in reopened.ready_pending_purchases()],
                         [purchase['purchase_id']])


class FakeUploader:
    async def upload(self, path, caption):
        return {'storage_chat_id': -1001, 'storage_message_id': 10}


class FakeBot:
    def __init__(self): self.calls = 0
    async def copy_message(self, **kwargs):
        self.calls += 1
        await asyncio.sleep(0.02)
        return SimpleNamespace(message_id=99)


class DeliveryConcurrencyTests(unittest.IsolatedAsyncioTestCase, BaseStoreTest):
    async def test_two_workers_copy_only_once(self):
        self.fund(1); offer = self.offer(); purchase, _, _ = self.store.create_download_purchase(1, offer)
        job = self.store.job_for_resource(offer['resource_id'])
        self.store.claim_download(job['job_id']); self.store.mark_uploading(job['job_id'], '/tmp/x')
        self.store.complete_download(job['job_id'], storage_chat_id=-1001,
                                     storage_message_id=2, file_size=1, checksum='sha256:x')
        bot = FakeBot()
        await asyncio.gather(deliver_purchase(self.store, bot, purchase['purchase_id']),
                             deliver_purchase(self.store, bot, purchase['purchase_id']))
        self.assertEqual(bot.calls, 1)
        self.assertEqual(self.store.get_purchase(purchase['purchase_id'])['status'], 'delivered')

    async def test_copy_success_db_failure_never_refunds(self):
        self.fund(1); offer = self.offer(); purchase, _, _ = self.store.create_download_purchase(1, offer)
        job = self.store.job_for_resource(offer['resource_id'])
        self.store.claim_download(job['job_id']); self.store.mark_uploading(job['job_id'], '/tmp/x')
        self.store.complete_download(job['job_id'], storage_chat_id=-1001,
                                     storage_message_id=2, file_size=1, checksum='sha256:x')
        bot = FakeBot()
        with patch.object(self.store, 'mark_delivered', side_effect=RuntimeError('sqlite down')):
            with self.assertRaises(DeliveryFailed):
                await deliver_purchase(self.store, bot, purchase['purchase_id'])
        self.assertEqual(self.store.get_balance_units(1), 200000000)
        self.assertEqual(self.store.get_purchase(purchase['purchase_id'])['status'], 'manual_review')
        self.assertEqual([x for x in self.store.ledger_for(1) if x['kind'] == 'refund'], [])

    async def test_each_job_uses_isolated_directory_and_cleans_it(self):
        self.fund(1); offer = self.offer(); self.store.create_download_purchase(1, offer)
        job = self.store.job_for_resource(offer['resource_id'])
        destinations = []
        def download(url, destination_dir, progress=None):
            destinations.append(destination_dir)
            p = Path(destination_dir) / 'same.zip'; p.write_bytes(b'x')
            return {'path': str(p), 'file_size': 1, 'checksum': 'sha256:x', 'final_url': url}
        with tempfile.TemporaryDirectory() as root:
            await process_download_job(self.store, job['job_id'], download,
                                       FakeUploader(), FakeBot(), root)
            self.assertIn(job['job_id'], Path(destinations[0]).name)
            self.assertFalse(Path(destinations[0]).exists())


class JobCleanupTests(unittest.TestCase):
    def test_only_stale_job_directories_are_removed(self):
        with tempfile.TemporaryDirectory() as root:
            old=Path(root)/'job-old'; fresh=Path(root)/'job-fresh'; unrelated=Path(root)/'keep'
            old.mkdir(); fresh.mkdir(); unrelated.mkdir()
            os.utime(old,(1,1))
            removed=cleanup_stale_job_dirs(root,older_than_seconds=60,now=1000)
            self.assertEqual(removed,1)
            self.assertFalse(old.exists()); self.assertTrue(fresh.exists()); self.assertTrue(unrelated.exists())


class DownloaderBoundaryTests(unittest.TestCase):
    def test_unknown_length_requires_maximum_capacity_plus_reserve(self):
        response = SimpleNamespace(status_code=200, headers={}, url='https://pixeldrain.com/api/file/X',
                                   raise_for_status=lambda: None, close=lambda: None)
        with tempfile.TemporaryDirectory() as tmp, \
             patch('downloader._secure_get', return_value=response), \
             patch('downloader.shutil.disk_usage', return_value=SimpleNamespace(free=109)):
            with self.assertRaises(DownloadError):
                download_file('https://pixeldrain.com/api/file/X', tmp,
                              max_bytes=100, reserve_bytes=10)


if __name__ == '__main__': unittest.main()
