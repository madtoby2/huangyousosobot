#!/usr/bin/env python3
"""SQLite wallet, top-up orders and append-only ledger."""
from __future__ import annotations

import sqlite3
import time
import uuid

from okaypay import decimal_to_units


class PaymentMismatch(RuntimeError):
    pass


class WalletStore:
    def __init__(self, path: str):
        self.path = path
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA foreign_keys=ON')
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS users (
                    tg_user_id INTEGER PRIMARY KEY,
                    balance_units INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS topup_orders (
                    order_id TEXT PRIMARY KEY,
                    tg_user_id INTEGER NOT NULL REFERENCES users(tg_user_id),
                    provider_order_id TEXT UNIQUE,
                    expected_units INTEGER NOT NULL,
                    amount_text TEXT NOT NULL,
                    asset TEXT NOT NULL,
                    payment_url TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    expires_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    credited_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS balance_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tg_user_id INTEGER NOT NULL REFERENCES users(tg_user_id),
                    order_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    delta_units INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    UNIQUE(order_id, kind)
                );
                CREATE INDEX IF NOT EXISTS idx_topup_pending
                    ON topup_orders(status, expires_at);
            ''')

    def create_topup(self, tg_user_id: int, amount: str, asset: str = 'USDT', lifetime: int = 1800):
        units = decimal_to_units(amount, 8)
        if units <= 0:
            raise ValueError('amount must be positive')
        now = int(time.time())
        order_id = uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('INSERT OR IGNORE INTO users(tg_user_id, created_at) VALUES(?, ?)',
                         (tg_user_id, now))
            conn.execute('''INSERT INTO topup_orders
                (order_id,tg_user_id,expected_units,amount_text,asset,expires_at,created_at)
                VALUES(?,?,?,?,?,?,?)''',
                (order_id, tg_user_id, units, amount, asset.upper(), now + lifetime, now))
            conn.commit()
        return self.get_order(order_id)

    def attach_provider(self, order_id: str, provider_order_id: str, payment_url: str):
        with self._connect() as conn:
            result = conn.execute('''UPDATE topup_orders
                SET provider_order_id=?, payment_url=?
                WHERE order_id=? AND status='pending' AND provider_order_id IS NULL''',
                (provider_order_id, payment_url, order_id))
            if result.rowcount != 1:
                raise PaymentMismatch('order cannot accept provider payment')
        return self.get_order(order_id)

    def get_order(self, order_id: str):
        with self._connect() as conn:
            row = conn.execute('SELECT * FROM topup_orders WHERE order_id=?', (order_id,)).fetchone()
        return dict(row) if row else None

    def pending_orders(self):
        now = int(time.time())
        with self._connect() as conn:
            rows = conn.execute('''SELECT * FROM topup_orders
                WHERE status='pending' AND provider_order_id IS NOT NULL AND expires_at>=?
                ORDER BY created_at''', (now,)).fetchall()
        return [dict(row) for row in rows]

    def credit_verified(self, payment: dict) -> bool:
        order_id = str(payment.get('order_id', ''))
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM topup_orders WHERE order_id=?', (order_id,)).fetchone()
            if not row:
                conn.rollback()
                raise PaymentMismatch('order not found')
            try:
                paid_units = decimal_to_units(payment.get('amount'), 8)
            except (TypeError, ValueError) as exc:
                conn.rollback()
                raise PaymentMismatch('invalid amount') from exc
            if (str(payment.get('provider_order_id', '')) != str(row['provider_order_id']) or
                    str(payment.get('coin', '')).upper() != row['asset'] or
                    paid_units != row['expected_units']):
                conn.rollback()
                raise PaymentMismatch('verified payment does not match order')
            if row['status'] == 'paid':
                conn.rollback()
                return False
            if row['status'] != 'pending':
                conn.rollback()
                raise PaymentMismatch('order is not pending')
            now = int(time.time())
            conn.execute('''INSERT INTO balance_ledger
                (tg_user_id,order_id,kind,delta_units,created_at) VALUES(?,?,?,?,?)''',
                (row['tg_user_id'], order_id, 'topup', row['expected_units'], now))
            conn.execute('UPDATE users SET balance_units=balance_units+? WHERE tg_user_id=?',
                         (row['expected_units'], row['tg_user_id']))
            conn.execute("UPDATE topup_orders SET status='paid', credited_at=? WHERE order_id=?",
                         (now, order_id))
            conn.commit()
            return True

    def get_balance_units(self, tg_user_id: int) -> int:
        with self._connect() as conn:
            row = conn.execute('SELECT balance_units FROM users WHERE tg_user_id=?',
                               (tg_user_id,)).fetchone()
        return int(row['balance_units']) if row else 0

    def ledger_for(self, tg_user_id: int):
        with self._connect() as conn:
            rows = conn.execute('SELECT * FROM balance_ledger WHERE tg_user_id=? ORDER BY id',
                                (tg_user_id,)).fetchall()
        return [dict(row) for row in rows]
