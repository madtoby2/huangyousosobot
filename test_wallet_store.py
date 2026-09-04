#!/usr/bin/env python3
import os
import tempfile
import unittest

from wallet_store import WalletStore, PaymentMismatch


class WalletStoreTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix='.sqlite3')
        os.close(handle)
        self.store = WalletStore(self.path)

    def tearDown(self):
        os.unlink(self.path)

    def test_create_topup_enforces_public_amount_limits(self):
        for invalid in ('0', '-1', '0.99999999', '10000.00000001',
                        '1.000000001', 'NaN', 'Infinity', '１'):
            with self.subTest(invalid=invalid):
                with self.assertRaises((TypeError, ValueError)):
                    self.store.create_topup(123, invalid, 'USDT')
        for valid in ('1', '1.00000000', '9999.99999999', '10000.00000000'):
            with self.subTest(valid=valid):
                self.assertEqual(self.store.create_topup(123, valid, 'USDT')['amount_text'], valid)

    def test_download_offer_accepts_valid_bt_magnet_only(self):
        offer = {
            'resource_id': 'bt:abc123', 'title': 'NHDTB-706', 'source': 'bt',
            'source_url': 'https://sukebei.nyaa.si/view/123',
            'download_url': 'magnet:?xt=urn:btih:47a51b8012cd969076ae0a3ae7c65465411a4e0c',
            'version': '47a51b8012cd969076ae0a3ae7c65465411a4e0c',
            'price_units': 100000000,
        }
        order = self.store.create_topup(123, '2', 'USDT')
        self.store.attach_provider(order['order_id'], 'provider-bt', 'https://pay.example/bt')
        self.store.credit_verified({'order_id': order['order_id'], 'provider_order_id': 'provider-bt',
                                    'coin': 'USDT', 'amount': '2'})
        purchase, charged, created = self.store.create_download_purchase(123, offer)
        self.assertTrue(charged)
        self.assertTrue(created)
        bad = dict(offer, resource_id='bt:bad', download_url='file:///etc/passwd')
        with self.assertRaises(ValueError):
            self.store.create_download_purchase(123, bad)

    def test_credit_is_idempotent_across_webhook_and_poll(self):
        order = self.store.create_topup(123, '2.50000000', 'USDT')
        self.store.attach_provider(order['order_id'], 'provider-1', 'https://pay.example/1')
        verified = {'order_id': order['order_id'], 'provider_order_id': 'provider-1',
                    'coin': 'USDT', 'amount': '2.50000000'}
        self.assertTrue(self.store.credit_verified(verified))
        self.assertFalse(self.store.credit_verified(verified))
        self.assertEqual(self.store.get_balance_units(123), 250000000)
        self.assertEqual(len(self.store.ledger_for(123)), 1)

    def test_mismatched_amount_coin_or_provider_does_not_credit(self):
        order = self.store.create_topup(123, '2.5', 'USDT')
        self.store.attach_provider(order['order_id'], 'provider-1', 'https://pay.example/1')
        for change in ({'amount': '2.4'}, {'coin': 'TRX'}, {'provider_order_id': 'other'}):
            payment = {'order_id': order['order_id'], 'provider_order_id': 'provider-1',
                       'coin': 'USDT', 'amount': '2.5'}
            payment.update(change)
            with self.assertRaises(PaymentMismatch):
                self.store.credit_verified(payment)
        self.assertEqual(self.store.get_balance_units(123), 0)

    def test_pending_orders_survive_restart(self):
        order = self.store.create_topup(456, '1', 'USDT')
        self.store.attach_provider(order['order_id'], 'provider-2', 'https://pay.example/2')
        reopened = WalletStore(self.path)
        pending = reopened.pending_orders()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['provider_order_id'], 'provider-2')
        self.assertEqual(pending[0]['tg_user_id'], 456)


if __name__ == '__main__':
    unittest.main()
