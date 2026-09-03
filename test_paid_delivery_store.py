#!/usr/bin/env python3
import os
import tempfile
import unittest

from artifacts import game_resource_id
from wallet_store import InsufficientBalance, WalletStore


class OnDemandPurchaseTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix='.sqlite3')
        os.close(fd)
        self.store = WalletStore(self.path)
        self.offer = {
            'resource_id': game_resource_id('ryuugames', 'https://ryuugames.com/test-game/'),
            'title': 'Test Game',
            'source': 'ryuugames',
            'source_url': 'https://ryuugames.com/test-game/',
            'download_url': 'https://download.example/test-game.zip',
            'version': '1.0',
            'price_units': 100000000,
        }

    def tearDown(self):
        for suffix in ('', '-shm', '-wal'):
            try: os.unlink(self.path + suffix)
            except FileNotFoundError: pass

    def fund(self, user_id, amount='5'):
        order = self.store.create_topup(user_id, amount, 'USDT')
        provider = 'provider-' + order['order_id']
        self.store.attach_provider(order['order_id'], provider, 'https://pay.example/x')
        self.store.credit_verified({'order_id': order['order_id'],
                                    'provider_order_id': provider,
                                    'coin': 'USDT', 'amount': amount})

    def test_paid_purchase_creates_one_queued_download_job(self):
        self.fund(101)
        purchase, charged, job_created = self.store.create_download_purchase(101, self.offer)
        self.assertTrue(charged)
        self.assertTrue(job_created)
        self.assertEqual(self.store.get_balance_units(101), 400000000)
        self.assertEqual(purchase['download_url'], self.offer['download_url'])
        job = self.store.job_for_resource(self.offer['resource_id'])
        self.assertEqual(job['status'], 'queued')

    def test_two_buyers_share_one_active_download(self):
        self.fund(101)
        self.fund(202)
        first = self.store.create_download_purchase(101, self.offer)
        second = self.store.create_download_purchase(202, self.offer)
        self.assertTrue(first[2])
        self.assertFalse(second[2])
        self.assertEqual(len(self.store.jobs_for_resource(self.offer['resource_id'])), 1)
        self.assertEqual(self.store.get_balance_units(101), 400000000)
        self.assertEqual(self.store.get_balance_units(202), 400000000)

    def test_repeat_click_does_not_charge_twice(self):
        self.fund(101)
        first = self.store.create_download_purchase(101, self.offer)
        second = self.store.create_download_purchase(101, self.offer)
        self.assertTrue(first[1])
        self.assertFalse(second[1])
        self.assertEqual(first[0]['purchase_id'], second[0]['purchase_id'])
        self.assertEqual(self.store.get_balance_units(101), 400000000)

    def test_insufficient_balance_creates_no_purchase_or_job(self):
        with self.assertRaises(InsufficientBalance):
            self.store.create_download_purchase(303, self.offer)
        self.assertEqual(self.store.purchases_for(303), [])
        self.assertEqual(self.store.jobs_for_resource(self.offer['resource_id']), [])

    def test_download_failure_refunds_all_waiting_buyers_once(self):
        self.fund(101)
        self.fund(202)
        self.store.create_download_purchase(101, self.offer)
        self.store.create_download_purchase(202, self.offer)
        job = self.store.job_for_resource(self.offer['resource_id'])
        self.assertEqual(self.store.fail_download(job['job_id'], 'HTTP 403'), 2)
        self.assertEqual(self.store.fail_download(job['job_id'], 'again'), 0)
        self.assertEqual(self.store.get_balance_units(101), 500000000)
        self.assertEqual(self.store.get_balance_units(202), 500000000)
        self.assertEqual(self.store.get_purchase_for(101, self.offer['resource_id'])['status'], 'refunded')
        self.assertEqual(self.store.get_purchase_for(202, self.offer['resource_id'])['status'], 'refunded')

    def test_ready_cache_skips_download_job_for_later_buyer(self):
        self.fund(101)
        self.store.create_download_purchase(101, self.offer)
        job = self.store.job_for_resource(self.offer['resource_id'])
        self.store.claim_download(job['job_id'])
        self.store.mark_uploading(job['job_id'], '/tmp/test')
        self.store.complete_download(job['job_id'], storage_chat_id=-100999,
                                     storage_message_id=88, file_size=1234,
                                     checksum='sha256:def')
        self.fund(202)
        purchase, charged, job_created = self.store.create_download_purchase(202, self.offer)
        self.assertTrue(charged)
        self.assertFalse(job_created)
        resource = self.store.get_resource(self.offer['resource_id'])
        self.assertEqual(resource['cache_status'], 'ready')
        self.assertEqual(resource['storage_message_id'], 88)
        self.assertEqual(purchase['status'], 'pending')


if __name__ == '__main__':
    unittest.main()
