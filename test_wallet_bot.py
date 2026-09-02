#!/usr/bin/env python3
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot import (_create_topup_checkout, _parse_topup_amount, _wallet_keyboard,
                 format_balance, start)
from wallet_store import WalletStore


class FakeClient:
    def __init__(self):
        self.calls = []
    def create_payment(self, order_id, amount, coin, callback_url, description):
        self.calls.append((order_id, amount, coin, callback_url, description))
        return {'provider_order_id': 'provider-1', 'payment_url': 'https://pay.example/1'}


class WalletBotTests(unittest.IsolatedAsyncioTestCase):
    def test_amount_parser_and_balance_format_are_exact(self):
        self.assertEqual(_parse_topup_amount('1.25'), '1.25')
        self.assertEqual(_parse_topup_amount('1'), '1')
        self.assertIsNone(_parse_topup_amount('0.99'))
        self.assertIsNone(_parse_topup_amount('1.123456789'))
        self.assertEqual(format_balance(125000000), '1.25')

    def test_wallet_keyboard_has_topup_button(self):
        buttons = [button for row in _wallet_keyboard().inline_keyboard for button in row]
        self.assertTrue(any(button.callback_data == 'wallet_topup' for button in buttons))

    def test_checkout_uses_public_callback_and_persists_provider_order(self):
        fd, path = tempfile.mkstemp(suffix='.sqlite3')
        os.close(fd)
        try:
            store, client = WalletStore(path), FakeClient()
            result = _create_topup_checkout(123, '2.5', store, client,
                                             'https://lifecheck.dpdns.org')
            self.assertEqual(client.calls[0][3], 'https://lifecheck.dpdns.org/api/okpay/notify')
            self.assertEqual(result['provider_order_id'], 'provider-1')
            self.assertEqual(result['tg_user_id'], 123)
        finally:
            os.unlink(path)

    async def test_start_includes_wallet_entry(self):
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(effective_user=SimpleNamespace(id=123), message=message)
        await start(update, None)
        keyboard = message.reply_text.await_args.kwargs['reply_markup']
        buttons = [button for row in keyboard.inline_keyboard for button in row]
        self.assertTrue(any(button.callback_data == 'wallet_home' for button in buttons))


if __name__ == '__main__':
    unittest.main()
