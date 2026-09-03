#!/usr/bin/env python3
import os
import tempfile
import unittest
from types import SimpleNamespace
from telegram.error import BadRequest

from artifacts import game_resource_id
from delivery import DeliveryFailed, deliver_purchase
from wallet_store import WalletStore


class FakeBot:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []
    async def copy_message(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise BadRequest('telegram copy failed')
        return SimpleNamespace(message_id=8080)


class CachedDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix='.sqlite3')
        os.close(fd)
        self.store = WalletStore(self.path)
        order = self.store.create_topup(123, '5', 'USDT')
        provider = 'provider-' + order['order_id']
        self.store.attach_provider(order['order_id'], provider, 'https://pay.example/x')
        self.store.credit_verified({'order_id': order['order_id'],
                                    'provider_order_id': provider,
                                    'coin': 'USDT', 'amount': '5'})
        self.resource_id = game_resource_id('ryuugames', 'https://ryuugames.com/test/')
        offer = {'resource_id': self.resource_id, 'title': 'Test Game',
                 'source': 'ryuugames', 'source_url': 'https://ryuugames.com/test/',
                 'download_url': 'https://download.example/test.zip',
                 'version': '1.0', 'price_units': 100000000}
        self.purchase, _, _ = self.store.create_download_purchase(123, offer)
        job = self.store.job_for_resource(self.resource_id)
        self.store.claim_download(job['job_id'])
        self.store.mark_uploading(job['job_id'], '/tmp/test')
        self.store.complete_download(job['job_id'], storage_chat_id=-1001234567890,
                                     storage_message_id=77, file_size=123,
                                     checksum='sha256:abc')

    def tearDown(self):
        for suffix in ('', '-shm', '-wal'):
            try: os.unlink(self.path + suffix)
            except FileNotFoundError: pass

    async def test_success_copies_ready_cache_and_records_message(self):
        bot = FakeBot()
        result = await deliver_purchase(self.store, bot, self.purchase['purchase_id'])
        self.assertTrue(result['delivered_now'])
        self.assertEqual(bot.calls, [{'chat_id': 123,
                                      'from_chat_id': -1001234567890,
                                      'message_id': 77}])
        self.assertEqual(self.store.get_purchase(self.purchase['purchase_id'])['status'], 'delivered')

    async def test_duplicate_delivery_is_idempotent(self):
        bot = FakeBot()
        await deliver_purchase(self.store, bot, self.purchase['purchase_id'])
        result = await deliver_purchase(self.store, bot, self.purchase['purchase_id'])
        self.assertFalse(result['delivered_now'])
        self.assertEqual(len(bot.calls), 1)

    async def test_claimed_delivery_is_not_copied_by_second_worker(self):
        self.assertTrue(self.store.claim_delivery(self.purchase['purchase_id']))
        bot = FakeBot()
        result = await deliver_purchase(self.store, bot, self.purchase['purchase_id'])
        self.assertFalse(result['delivered_now'])
        self.assertTrue(result['in_progress'])
        self.assertEqual(bot.calls, [])

    async def test_copy_failure_refunds_only_this_purchase_once(self):
        bot = FakeBot(fail=True)
        with self.assertRaises(DeliveryFailed):
            await deliver_purchase(self.store, bot, self.purchase['purchase_id'])
        self.assertEqual(self.store.get_balance_units(123), 500000000)
        with self.assertRaises(DeliveryFailed):
            await deliver_purchase(self.store, bot, self.purchase['purchase_id'])
        refunds = [x for x in self.store.ledger_for(123) if x['kind'] == 'refund']
        self.assertEqual(len(refunds), 1)


if __name__ == '__main__':
    unittest.main()
