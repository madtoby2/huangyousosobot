#!/usr/bin/env python3
import json
import os
import tempfile
import unittest

from okaypay import OkayPayClient, sign_payload
from wallet_store import WalletStore
from webhook_server import handle_notification


class WebhookTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix='.sqlite3')
        os.close(handle)
        self.store = WalletStore(self.path)
        self.order = self.store.create_topup(777, '1.25', 'USDT')
        self.store.attach_provider(self.order['order_id'], 'provider-7', 'https://pay.example/7')
        self.client = OkayPayClient('40109', 'secret', transport=lambda *_: {})
        self.client.check_payment = lambda _: {
            'order_id': self.order['order_id'], 'provider_order_id': 'provider-7',
            'coin': 'USDT', 'amount': '1.25'}

    def tearDown(self):
        os.unlink(self.path)

    def payload(self):
        value = {'status': 'success', 'code': 200, 'id': 40109,
                 'data': {'amount': '1.25', 'coin': 'USDT', 'order_id': 'provider-7',
                          'status': 1, 'type': 'deposit', 'unique_id': self.order['order_id']}}
        value['sign'] = sign_payload(value, 'secret')
        return value

    def test_valid_callback_requeries_and_credits_once(self):
        body = json.dumps(self.payload()).encode()
        status, result = handle_notification(body, 'application/json', self.client, self.store)
        self.assertEqual(status, 200)
        self.assertTrue(result['credited'])
        status, result = handle_notification(body, 'application/json', self.client, self.store)
        self.assertEqual(status, 200)
        self.assertFalse(result['credited'])
        self.assertEqual(self.store.get_balance_units(777), 125000000)

    def test_forged_callback_is_rejected(self):
        payload = self.payload()
        payload['sign'] = '0' * 64
        status, result = handle_notification(json.dumps(payload).encode(), 'application/json', self.client, self.store)
        self.assertEqual(status, 401)
        self.assertEqual(self.store.get_balance_units(777), 0)

    def test_malformed_and_oversized_bodies_are_rejected(self):
        self.assertEqual(handle_notification(b'{', 'application/json', self.client, self.store)[0], 400)
        self.assertEqual(handle_notification(b'x' * 65537, 'application/json', self.client, self.store)[0], 413)

    def test_callback_fields_cannot_override_provider_requery(self):
        payload = self.payload()
        payload['data']['amount'] = '999'
        payload['sign'] = sign_payload(payload, 'secret')
        status, _ = handle_notification(json.dumps(payload).encode(), 'application/json', self.client, self.store)
        self.assertEqual(status, 422)
        self.assertEqual(self.store.get_balance_units(777), 0)


if __name__ == '__main__':
    unittest.main()
