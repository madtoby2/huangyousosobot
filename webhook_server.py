#!/usr/bin/env python3
"""OkayPay webhook listener bound to localhost behind Caddy."""
from __future__ import annotations

import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs
from urllib.request import Request, urlopen

from dotenv import load_dotenv

from okaypay import OkayPayClient, OkayPayError, decimal_to_units
from wallet_store import PaymentMismatch, WalletStore

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))
logger = logging.getLogger('searchbot.webhook')
MAX_BODY = 64 * 1024


def _decode_body(body: bytes, content_type: str):
    if len(body) > MAX_BODY:
        return None, 413
    try:
        if 'application/json' in content_type:
            value = json.loads(body.decode('utf-8'))
        else:
            parsed = parse_qs(body.decode('utf-8'), keep_blank_values=True)
            value = {key: values[-1] for key, values in parsed.items()}
        return (value, None) if isinstance(value, dict) else (None, 400)
    except (UnicodeDecodeError, ValueError):
        return None, 400


def handle_notification(body: bytes, content_type: str, client: OkayPayClient,
                        store: WalletStore):
    payload, error = _decode_body(body, content_type)
    if error:
        return error, {'error': 'payload_too_large' if error == 413 else 'invalid_notification'}
    if payload.get('status') != 'success' or str(payload.get('code')) != '200':
        return 400, {'error': 'invalid_notification'}
    if not client.verify(payload):
        return 401, {'error': 'invalid_signature'}
    try:
        source = client.parse_callback(payload)
    except OkayPayError:
        return 400, {'error': 'invalid_notification'}
    if str(source.get('type')) != 'deposit' or str(source.get('status')) != '1':
        return 400, {'error': 'invalid_notification'}
    order_id = str(source.get('unique_id', '')).strip()
    order = store.get_order(order_id)
    if not order:
        return 404, {'error': 'order_not_found'}
    try:
        callback_units = decimal_to_units(source.get('amount'), 8)
    except (TypeError, ValueError):
        return 422, {'error': 'payment_mismatch'}
    if (str(source.get('order_id', '')).strip() != str(order['provider_order_id']) or
            str(source.get('coin', '')).upper() != order['asset'] or
            callback_units != order['expected_units']):
        return 422, {'error': 'payment_mismatch'}
    try:
        payment = client.check_payment(order['provider_order_id'])
        if not payment:
            return 409, {'error': 'payment_pending'}
        credited = store.credit_verified(payment)
    except PaymentMismatch:
        return 422, {'error': 'payment_mismatch'}
    except OkayPayError:
        return 502, {'error': 'provider_unavailable'}
    return 200, {'success': True, 'credited': credited, 'tg_user_id': order['tg_user_id'],
                 'amount': order['amount_text'], 'asset': order['asset']}


def notify_telegram(tg_user_id: int, amount: str, asset: str):
    token = os.environ.get('BOT_TOKEN', '')
    if not token:
        return
    body = json.dumps({'chat_id': tg_user_id,
                       'text': f'✅ 充值成功：{amount} {asset}\n发送 /wallet 查看余额。'}).encode()
    request = Request(f'https://api.telegram.org/bot{token}/sendMessage', data=body,
                      headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urlopen(request, timeout=10) as response:
            response.read()
    except Exception:
        logger.exception('Telegram top-up notification failed')


class Handler(BaseHTTPRequestHandler):
    client = None
    store = None

    def do_POST(self):
        if self.path != '/api/okpay/notify':
            self.send_error(404)
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            size = MAX_BODY + 1
        if size > MAX_BODY:
            status, result = 413, {'error': 'payload_too_large'}
        else:
            body = self.rfile.read(size)
            status, result = handle_notification(
                body, self.headers.get('Content-Type', ''), self.client, self.store)
        encoded = json.dumps(result, separators=(',', ':')).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)
        if status == 200 and result.get('credited'):
            notify_telegram(result['tg_user_id'], result['amount'], result['asset'])

    def log_message(self, fmt, *args):
        logger.info('%s - %s', self.client_address[0], fmt % args)


def main():
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    shop_id = os.environ.get('OKPAY_SHOP_ID', '')
    api_key = os.environ.get('OKPAY_API_KEY', '')
    db_path = os.environ.get('WALLET_DB', os.path.join(os.path.dirname(__file__), 'wallet.sqlite3'))
    Handler.client = OkayPayClient(shop_id, api_key)
    Handler.store = WalletStore(db_path)
    server = ThreadingHTTPServer(('127.0.0.1', 8765), Handler)
    logger.info('OkayPay webhook listening on 127.0.0.1:8765')
    server.serve_forever()


if __name__ == '__main__':
    main()
