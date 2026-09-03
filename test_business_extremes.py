#!/usr/bin/env python3
import asyncio
import os
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from artifacts import game_resource_id
from delivery import DeliveryFailed, DeliveryNotReady, deliver_purchase
from downloader import UnsupportedDownloadHost, _one_pinned_get, _resolve_public
from wallet_store import PaymentMismatch, WalletStore


class BusinessStressTests(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix='.sqlite3'); os.close(fd)
        self.store = WalletStore(self.db)

    def tearDown(self):
        for suffix in ('', '-wal', '-shm'):
            try: os.unlink(self.db + suffix)
            except FileNotFoundError: pass

    def fund(self, user, amount='5'):
        order = self.store.create_topup(user, amount, 'USDT')
        provider = 'p-' + order['order_id']
        self.store.attach_provider(order['order_id'], provider, 'https://pay.example/x')
        payment = {'order_id': order['order_id'], 'provider_order_id': provider,
                   'coin': 'USDT', 'amount': amount}
        self.assertTrue(self.store.credit_verified(payment))
        return payment

    @staticmethod
    def offer(version='1', mirror='ONE'):
        page = 'https://ryuugames.com/game/'
        url = 'https://pixeldrain.com/u/' + mirror
        return {'resource_id': game_resource_id('ryuugames', page, version, url),
                'title': 'Game', 'source': 'ryuugames', 'source_url': page,
                'download_url': url, 'version': version, 'price_units': 100000000}

    def test_twenty_simultaneous_clicks_charge_one_time(self):
        self.fund(1); offer = self.offer(); outcomes = []
        threads = [threading.Thread(target=lambda: outcomes.append(
            self.store.create_download_purchase(1, offer))) for _ in range(20)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(sum(int(x[1]) for x in outcomes), 1)
        self.assertEqual(len(self.store.purchases_for(1)), 1)
        self.assertEqual(self.store.get_balance_units(1), 400000000)

    def test_ten_buyers_coalesce_to_one_job(self):
        offer = self.offer(); threads = []
        for user in range(10): self.fund(user + 100)
        for user in range(10):
            threads.append(threading.Thread(target=self.store.create_download_purchase,
                                            args=(user + 100, offer)))
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(len(self.store.jobs_for_resource(offer['resource_id'])), 1)
        self.assertEqual(sum(len(self.store.purchases_for(x + 100)) for x in range(10)), 10)

    def test_concurrent_payment_credit_is_idempotent(self):
        order = self.store.create_topup(1, '5', 'USDT'); provider = 'p-' + order['order_id']
        self.store.attach_provider(order['order_id'], provider, 'https://pay.example/x')
        payment = {'order_id': order['order_id'], 'provider_order_id': provider,
                   'coin': 'USDT', 'amount': '5'}
        results = []
        threads = [threading.Thread(target=lambda: results.append(
            self.store.credit_verified(payment))) for _ in range(20)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(sum(map(int, results)), 1)
        self.assertEqual(self.store.get_balance_units(1), 500000000)

    def test_new_version_never_reuses_old_ready_cache(self):
        self.fund(1); old = self.offer('1', 'ONE')
        self.store.create_download_purchase(1, old)
        job = self.store.job_for_resource(old['resource_id'])
        claimed = self.store.claim_download(job['job_id'])
        self.store.mark_uploading(job['job_id'], '/tmp/a', claimed['lease_token'])
        self.store.complete_download(job['job_id'], storage_chat_id=-1001,
                                     storage_message_id=8, file_size=1, checksum='sha256:a',
                                     lease_token=claimed['lease_token'])
        self.fund(2); new = self.offer('2', 'TWO')
        _, _, created = self.store.create_download_purchase(2, new)
        self.assertTrue(created)
        self.assertNotEqual(old['resource_id'], new['resource_id'])
        self.assertEqual(self.store.get_resource(new['resource_id'])['cache_status'], 'queued')

    def test_manual_review_repeat_click_does_not_charge_again(self):
        self.fund(1); offer=self.offer(); purchase,_,_=self.store.create_download_purchase(1,offer)
        self.store.claim_delivery(purchase['purchase_id'])
        self.store.mark_delivery_unknown(purchase['purchase_id'],'timeout')
        again,charged,created=self.store.create_download_purchase(1,offer)
        self.assertEqual(again['purchase_id'],purchase['purchase_id'])
        self.assertFalse(charged); self.assertFalse(created)
        self.assertEqual(self.store.get_balance_units(1),400000000)

    def test_expired_worker_cannot_finish_reclaimed_job(self):
        self.fund(1); offer = self.offer(); self.store.create_download_purchase(1, offer)
        job = self.store.job_for_resource(offer['resource_id'])
        first = self.store.claim_download(job['job_id'])
        self.store.reset_interrupted_downloads(now=first['lease_expires_at'] + 1)
        second = self.store.claim_download(job['job_id'])
        with self.assertRaises(PaymentMismatch):
            self.store.mark_uploading(job['job_id'], '/tmp/stale', first['lease_token'])
        self.store.mark_uploading(job['job_id'], '/tmp/current', second['lease_token'])


class DeliveryUnknownTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix='.sqlite3'); os.close(fd)
        self.store = WalletStore(self.db)
        order = self.store.create_topup(1, '2', 'USDT'); provider = 'p-' + order['order_id']
        self.store.attach_provider(order['order_id'], provider, 'https://pay.example/x')
        self.store.credit_verified({'order_id': order['order_id'], 'provider_order_id': provider,
                                    'coin': 'USDT', 'amount': '2'})
        page='https://ryuugames.com/g/'; url='https://pixeldrain.com/u/A'
        offer={'resource_id':game_resource_id('ryuugames',page,'1',url),'title':'G',
               'source':'ryuugames','source_url':page,'download_url':url,
               'version':'1','price_units':100000000}
        self.purchase,_,_=self.store.create_download_purchase(1,offer)

    def tearDown(self):
        for suffix in ('','-wal','-shm'):
            try: os.unlink(self.db+suffix)
            except FileNotFoundError: pass

    async def test_not_ready_does_not_claim_or_refund(self):
        with self.assertRaises(DeliveryNotReady):
            await deliver_purchase(self.store, SimpleNamespace(), self.purchase['purchase_id'])
        self.assertEqual(self.store.get_purchase(self.purchase['purchase_id'])['status'], 'pending')
        self.assertEqual(self.store.get_balance_units(1), 100000000)

    async def test_network_timeout_becomes_manual_review_without_refund(self):
        job=self.store.job_for_resource(self.purchase['resource_id']); claim=self.store.claim_download(job['job_id'])
        self.store.mark_uploading(job['job_id'],'/tmp/a',claim['lease_token'])
        self.store.complete_download(job['job_id'],storage_chat_id=-1001,storage_message_id=2,
            file_size=1,checksum='sha256:x',lease_token=claim['lease_token'])
        class Bot:
            async def copy_message(self, **kwargs): raise TimeoutError('socket timeout')
        with self.assertRaises(DeliveryFailed):
            await deliver_purchase(self.store,Bot(),self.purchase['purchase_id'])
        self.assertEqual(self.store.get_purchase(self.purchase['purchase_id'])['status'],'manual_review')
        self.assertEqual(self.store.get_balance_units(1),100000000)
        self.assertEqual([x for x in self.store.ledger_for(1) if x['kind']=='refund'],[])


class SSRFPinningTests(unittest.TestCase):
    def test_any_non_global_dns_answer_rejects_host(self):
        answers=[(2,1,6,'',('1.1.1.1',443)),(2,1,6,'',('169.254.169.254',443))]
        with patch('downloader.socket.getaddrinfo',return_value=answers):
            with self.assertRaises(UnsupportedDownloadHost):
                _resolve_public('https://pixeldrain.com/api/file/X')

    def test_validated_address_is_pinned_into_transport(self):
        pinned=[]
        def fake_get(session,url,**kwargs):
            pinned.append(session.adapters['https://'].pinned_address)
            raise RuntimeError('stop before network')
        answers=[(2,1,6,'',('1.1.1.1',443))]
        with patch('downloader.socket.getaddrinfo',return_value=answers), \
             patch('downloader.requests.Session.get',fake_get):
            with self.assertRaises(RuntimeError): _one_pinned_get('https://pixeldrain.com/x')
        self.assertEqual(pinned,['1.1.1.1'])


if __name__=='__main__': unittest.main()
