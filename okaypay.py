#!/usr/bin/env python3
"""Minimal OkayPay hosted-checkout adapter."""
from __future__ import annotations

import hashlib
import hmac
import re
import time
import uuid
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

import httpx


class OkayPayError(RuntimeError):
    pass


def _flatten(value, prefix=''):
    fields = []
    for key, item in value.items():
        if key == 'sign' or item is None or item == '':
            continue
        path = f'{prefix}.{key}' if prefix else key
        if isinstance(item, dict):
            fields.extend(_flatten(item, path))
        elif isinstance(item, bool):
            fields.append((path, 'true' if item else 'false'))
        elif isinstance(item, (str, int)) and not isinstance(item, float):
            fields.append((path, str(item)))
        else:
            raise OkayPayError('unsupported value in signed payload')
    return fields


def sign_payload(payload: dict, api_key: str) -> str:
    base = '&'.join(f'{key}={value}' for key, value in sorted(_flatten(payload)))
    return hmac.new(api_key.encode(), base.encode(), hashlib.sha256).hexdigest().upper()


def decimal_to_units(value: str, decimals: int = 8) -> int:
    if not isinstance(value, str) or not re.fullmatch(r'\d+(?:\.\d+)?', value.strip()):
        raise TypeError('amount must be an unsigned decimal string')
    try:
        number = Decimal(value)
        units = number * (Decimal(10) ** decimals)
    except InvalidOperation as exc:
        raise ValueError('invalid decimal amount') from exc
    if units != units.to_integral_value():
        raise ValueError('too many decimal places')
    return int(units)


def _safe_https_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return parsed.scheme == 'https' and bool(parsed.hostname) and not parsed.username and not parsed.password
    except ValueError:
        return False


class OkayPayClient:
    def __init__(self, shop_id: str, api_key: str,
                 api_url: str = 'https://api.okaypay.me/shop', transport=None):
        self.shop_id = str(shop_id).strip()
        self.api_key = api_key.strip()
        self.api_url = api_url.rstrip('/')
        self.transport = transport or self._post_http
        if not self.shop_id or not self.api_key:
            raise ValueError('OkayPay credentials are required')

    def verify(self, payload: dict) -> bool:
        if str(payload.get('id', '')).strip() != self.shop_id:
            return False
        supplied = str(payload.get('sign', '')).upper()
        try:
            expected = sign_payload(payload, self.api_key)
        except OkayPayError:
            return False
        return len(supplied) == 64 and hmac.compare_digest(supplied, expected)

    def _post_http(self, url: str, data: dict) -> dict:
        try:
            response = httpx.post(url, data=data, timeout=8.0)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            raise OkayPayError('OkayPay network error') from exc
        if not isinstance(payload, dict):
            raise OkayPayError('invalid OkayPay response')
        return payload

    def _post(self, path: str, data: dict) -> dict:
        fields = {key: str(value) for key, value in data.items() if value is not None and value != ''}
        fields.update(id=self.shop_id, timestamp=str(int(time.time())), nonce=str(uuid.uuid4()))
        fields['sign'] = sign_payload(fields, self.api_key)
        payload = self.transport(f'{self.api_url}/{path}', fields)
        if not isinstance(payload, dict) or payload.get('status') != 'success' or str(payload.get('code')) != '200':
            raise OkayPayError('OkayPay rejected request')
        if not self.verify(payload):
            raise OkayPayError('invalid OkayPay response signature')
        return payload

    def create_payment(self, order_id: str, amount: str, coin: str,
                       callback_url: str, description: str = 'Balance top-up',
                       return_url: str | None = None) -> dict:
        payload = self._post('payLink', {
            'amount': amount, 'coin': coin.upper(), 'callback_url': callback_url,
            'name': description, 'return_url': return_url, 'unique_id': order_id,
        })
        data = payload.get('data')
        if not isinstance(data, dict):
            raise OkayPayError('invalid OkayPay payment response')
        provider_order_id = str(data.get('order_id', '')).strip()
        payment_url = str(data.get('pay_url', '')).strip()
        if not provider_order_id or not _safe_https_url(payment_url):
            raise OkayPayError('OkayPay did not return a safe payment URL')
        return {'provider_order_id': provider_order_id, 'payment_url': payment_url}

    def check_payment(self, provider_order_id: str) -> dict | None:
        payload = self._post('checkTransferByTxid', {'txid': provider_order_id})
        data = payload.get('data')
        if not isinstance(data, dict) or str(data.get('status')) != '1':
            return None
        amount = data.get('amount')
        if not isinstance(amount, str) or decimal_to_units(amount) <= 0:
            raise OkayPayError('invalid completed payment amount')
        return {
            'provider_order_id': str(data.get('order_id', '')).strip(),
            'order_id': str(data.get('unique_id', '')).strip(),
            'coin': str(data.get('coin', '')).upper(),
            'amount': amount,
        }

    def parse_callback(self, payload: dict) -> dict:
        data = payload.get('data', payload)
        if isinstance(data, str):
            import json
            try:
                data = json.loads(data)
            except ValueError as exc:
                raise OkayPayError('invalid callback data') from exc
        if not isinstance(data, dict):
            raise OkayPayError('invalid callback data')
        return data
