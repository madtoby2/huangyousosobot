#!/usr/bin/env python3
import json
import unittest

from okaypay import OkayPayClient, OkayPayError, decimal_to_units, sign_payload


class FakeResponse:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload
    def json(self):
        return self._payload


class OkayPayTests(unittest.TestCase):
    def test_official_nested_signature_vector(self):
        payload = {
            'status': 'success', 'code': 200,
            'data': {
                'amount': '100.5', 'coin': 'USDT', 'order_id': 'abc123def456',
                'pay_user_id': 123456789, 'status': 1, 'type': 'deposit',
                'unique_id': 'ORDER-20260628-001',
            },
            'id': 10001,
        }
        self.assertEqual(
            sign_payload(payload, 'TESTtoken123456789abcdefghijABCD'),
            '64B09C8847849FA6921D8FFBDF8E406D4A8EA623E53970712350F61783403F7D',
        )

    def test_signature_preserves_zero_and_false(self):
        payload = {'a': '0', 'b': 0, 'c': '', 'd': None, 'e': False, 'f': 'hello',
                   'id': 7, 'nest': {'x': '1', 'y': '2'}}
        self.assertEqual(
            sign_payload(payload, 'TESTtoken123456789abcdefghijABCD'),
            '8BC0AF979075038025DDD51B6F4A2E6CF3FF9B5B5371EB2268D303F89883E92A',
        )

    def test_decimal_conversion_is_exact_and_rejects_float(self):
        self.assertEqual(decimal_to_units('3.50000000', 8), 350000000)
        with self.assertRaises((TypeError, ValueError)):
            decimal_to_units(3.5, 8)

    def test_create_payment_signs_request_and_rejects_unsafe_url(self):
        calls = []
        client = OkayPayClient('40109', 'secret', transport=lambda url, data: calls.append((url, data)) or {
            'status': 'success', 'code': 200, 'id': 40109,
            'data': {'order_id': 'provider-1', 'pay_url': 'javascript:alert(1)', 'status': 0},
        })
        response = calls
        with self.assertRaises(OkayPayError):
            client.create_payment('local-1', '1.00', 'USDT', 'https://merchant.example/api/okpay/notify')
        self.assertEqual(calls[0][0], 'https://api.okaypay.me/shop/payLink')
        self.assertRegex(calls[0][1]['sign'], r'^[A-F0-9]{64}$')

    def test_callback_signature_must_match_shop(self):
        client = OkayPayClient('40109', 'secret', transport=lambda *_: {})
        payload = {'status': 'success', 'code': 200, 'id': 40109,
                   'data': {'amount': '1', 'coin': 'USDT', 'order_id': 'p1',
                            'status': 1, 'type': 'deposit', 'unique_id': 'l1'}}
        payload['sign'] = sign_payload(payload, 'secret')
        self.assertTrue(client.verify(payload))
        payload['id'] = 999
        self.assertFalse(client.verify(payload))


if __name__ == '__main__':
    unittest.main()
