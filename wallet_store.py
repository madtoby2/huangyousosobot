#!/usr/bin/env python3
"""SQLite wallet, on-demand download purchases and append-only ledger."""
from __future__ import annotations

import sqlite3
import time
import uuid
from urllib.parse import urlsplit

from okaypay import decimal_to_units


class PaymentMismatch(RuntimeError):
    pass


class InsufficientBalance(RuntimeError):
    pass


class ArtifactUnavailable(RuntimeError):
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
                CREATE TABLE IF NOT EXISTS resources (
                    resource_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    download_url TEXT NOT NULL,
                    version TEXT NOT NULL,
                    price_units INTEGER NOT NULL CHECK(price_units > 0),
                    cache_status TEXT NOT NULL DEFAULT 'missing',
                    storage_chat_id INTEGER,
                    storage_message_id INTEGER,
                    file_size INTEGER,
                    checksum TEXT,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS download_jobs (
                    job_id TEXT PRIMARY KEY,
                    resource_id TEXT NOT NULL REFERENCES resources(resource_id),
                    status TEXT NOT NULL DEFAULT 'queued',
                    error_text TEXT,
                    local_path TEXT,
                    created_at INTEGER NOT NULL,
                    started_at INTEGER,
                    completed_at INTEGER,
                    lease_token TEXT,
                    lease_expires_at INTEGER,
                    attempt_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_active_download_job
                    ON download_jobs(resource_id)
                    WHERE status IN ('queued','downloading','uploading');
                CREATE TABLE IF NOT EXISTS download_purchases (
                    purchase_id TEXT PRIMARY KEY,
                    tg_user_id INTEGER NOT NULL REFERENCES users(tg_user_id),
                    resource_id TEXT NOT NULL REFERENCES resources(resource_id),
                    title TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    download_url TEXT NOT NULL,
                    version TEXT NOT NULL,
                    price_units INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    telegram_message_id INTEGER,
                    error_text TEXT,
                    created_at INTEGER NOT NULL,
                    delivered_at INTEGER,
                    refunded_at INTEGER
                );
                DROP INDEX IF EXISTS idx_active_download_purchase;
                CREATE UNIQUE INDEX idx_active_download_purchase
                    ON download_purchases(tg_user_id, resource_id)
                    WHERE status IN ('pending','delivering','manual_review','delivered');
                CREATE INDEX IF NOT EXISTS idx_topup_pending
                    ON topup_orders(status, expires_at);
                CREATE INDEX IF NOT EXISTS idx_download_purchase_pending
                    ON download_purchases(resource_id,status,created_at);
                CREATE TRIGGER IF NOT EXISTS validate_download_job_status_insert
                BEFORE INSERT ON download_jobs WHEN NEW.status NOT IN
                    ('queued','downloading','uploading','ready','failed')
                BEGIN SELECT RAISE(ABORT,'invalid download job status'); END;
                CREATE TRIGGER IF NOT EXISTS validate_download_job_status_update
                BEFORE UPDATE OF status ON download_jobs WHEN NEW.status NOT IN
                    ('queued','downloading','uploading','ready','failed')
                BEGIN SELECT RAISE(ABORT,'invalid download job status'); END;
                CREATE TRIGGER IF NOT EXISTS validate_purchase_status_insert
                BEFORE INSERT ON download_purchases WHEN NEW.status NOT IN
                    ('pending','delivering','manual_review','delivered','refunded')
                BEGIN SELECT RAISE(ABORT,'invalid purchase status'); END;
                CREATE TRIGGER IF NOT EXISTS validate_purchase_status_update
                BEFORE UPDATE OF status ON download_purchases WHEN NEW.status NOT IN
                    ('pending','delivering','manual_review','delivered','refunded')
                BEGIN SELECT RAISE(ABORT,'invalid purchase status'); END;
            ''')
            columns = {row['name'] for row in conn.execute('PRAGMA table_info(download_jobs)')}
            for name, definition in (('lease_token', 'TEXT'), ('lease_expires_at', 'INTEGER'),
                                     ('attempt_count', 'INTEGER NOT NULL DEFAULT 0')):
                if name not in columns:
                    conn.execute(f'ALTER TABLE download_jobs ADD COLUMN {name} {definition}')

    def create_topup(self, tg_user_id: int, amount: str, asset: str = 'USDT', lifetime: int = 1800):
        units = decimal_to_units(amount, 8)
        if units < 100_000_000 or units > 1_000_000_000_000:
            raise ValueError('amount must be between 1 and 10000 USDT')
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
            result = conn.execute('''UPDATE topup_orders SET provider_order_id=?,payment_url=?
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
                conn.rollback(); raise PaymentMismatch('order not found')
            try:
                paid_units = decimal_to_units(payment.get('amount'), 8)
            except (TypeError, ValueError) as exc:
                conn.rollback(); raise PaymentMismatch('invalid amount') from exc
            if (str(payment.get('provider_order_id', '')) != str(row['provider_order_id']) or
                    str(payment.get('coin', '')).upper() != row['asset'] or
                    paid_units != row['expected_units']):
                conn.rollback(); raise PaymentMismatch('verified payment does not match order')
            if row['status'] == 'paid':
                conn.rollback(); return False
            if row['status'] != 'pending':
                conn.rollback(); raise PaymentMismatch('order is not pending')
            now = int(time.time())
            conn.execute('''INSERT INTO balance_ledger
                (tg_user_id,order_id,kind,delta_units,created_at) VALUES(?,?,?,?,?)''',
                (row['tg_user_id'], order_id, 'topup', row['expected_units'], now))
            conn.execute('UPDATE users SET balance_units=balance_units+? WHERE tg_user_id=?',
                         (row['expected_units'], row['tg_user_id']))
            conn.execute("UPDATE topup_orders SET status='paid',credited_at=? WHERE order_id=?",
                         (now, order_id))
            conn.commit(); return True

    @staticmethod
    def _validate_offer(offer: dict):
        required = ('resource_id','title','source','source_url','download_url','version','price_units')
        if any(not offer.get(key) for key in required):
            raise ValueError('incomplete download offer')
        if int(offer['price_units']) <= 0:
            raise ValueError('price must be positive')
        for key in ('source_url','download_url'):
            if urlsplit(str(offer[key])).scheme not in ('http','https'):
                raise ValueError(f'{key} must use HTTP(S)')

    def create_download_purchase(self, tg_user_id: int, offer: dict):
        self._validate_offer(offer)
        now = int(time.time())
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('INSERT OR IGNORE INTO users(tg_user_id,created_at) VALUES(?,?)',
                         (tg_user_id, now))
            existing = conn.execute('''SELECT * FROM download_purchases
                WHERE tg_user_id=? AND resource_id=? AND status IN ('pending','delivering','manual_review','delivered')
                ORDER BY created_at DESC LIMIT 1''',
                (tg_user_id, offer['resource_id'])).fetchone()
            if existing:
                conn.rollback()
                return dict(existing), False, False
            existing_resource = conn.execute(
                'SELECT * FROM resources WHERE resource_id=?', (offer['resource_id'],)).fetchone()
            immutable = ('source', 'source_url', 'download_url', 'version')
            if existing_resource and any(str(existing_resource[k]) != str(offer[k]) for k in immutable):
                conn.rollback(); raise PaymentMismatch('resource identity collision')
            price = (int(existing_resource['price_units']) if existing_resource
                     else int(offer['price_units']))
            balance = conn.execute('SELECT balance_units FROM users WHERE tg_user_id=?',
                                   (tg_user_id,)).fetchone()['balance_units']
            if int(balance) < price:
                conn.rollback(); raise InsufficientBalance('insufficient balance')
            if existing_resource:
                conn.execute('UPDATE resources SET title=?,updated_at=? WHERE resource_id=?',
                             (offer['title'], now, offer['resource_id']))
            else:
                conn.execute("""INSERT INTO resources
                    (resource_id,title,source,source_url,download_url,version,price_units,
                     cache_status,updated_at) VALUES(?,?,?,?,?,?,?,'missing',?)""",
                    (offer['resource_id'], offer['title'], offer['source'], offer['source_url'],
                     offer['download_url'], offer['version'], price, now))
            purchase_id = uuid.uuid4().hex
            conn.execute('''INSERT INTO download_purchases
                (purchase_id,tg_user_id,resource_id,title,source,source_url,download_url,
                 version,price_units,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,'pending',?)''',
                (purchase_id, tg_user_id, offer['resource_id'], offer['title'], offer['source'],
                 offer['source_url'], offer['download_url'], offer['version'], price, now))
            conn.execute('''INSERT INTO balance_ledger
                (tg_user_id,order_id,kind,delta_units,created_at) VALUES(?,?,?,?,?)''',
                (tg_user_id, purchase_id, 'purchase', -price, now))
            conn.execute('UPDATE users SET balance_units=balance_units-? WHERE tg_user_id=?',
                         (price, tg_user_id))
            resource = conn.execute('SELECT * FROM resources WHERE resource_id=?',
                                    (offer['resource_id'],)).fetchone()
            ready = (resource['cache_status'] == 'ready' and resource['storage_chat_id'] and
                     resource['storage_message_id'])
            job_created = False
            if not ready:
                active = conn.execute('''SELECT job_id FROM download_jobs WHERE resource_id=?
                    AND status IN ('queued','downloading','uploading') LIMIT 1''',
                    (offer['resource_id'],)).fetchone()
                if not active:
                    job_id = uuid.uuid4().hex
                    conn.execute('''INSERT INTO download_jobs(job_id,resource_id,status,created_at)
                                    VALUES(?,?,'queued',?)''',
                                 (job_id, offer['resource_id'], now))
                    conn.execute("UPDATE resources SET cache_status='queued' WHERE resource_id=?",
                                 (offer['resource_id'],))
                    job_created = True
            conn.commit()
        return self.get_purchase(purchase_id), True, job_created

    def get_resource(self, resource_id: str):
        with self._connect() as conn:
            row = conn.execute('SELECT * FROM resources WHERE resource_id=?', (resource_id,)).fetchone()
        return dict(row) if row else None

    def jobs_for_resource(self, resource_id: str):
        with self._connect() as conn:
            rows = conn.execute('''SELECT * FROM download_jobs WHERE resource_id=?
                                   ORDER BY created_at,job_id''', (resource_id,)).fetchall()
        return [dict(row) for row in rows]

    def job_for_resource(self, resource_id: str):
        jobs = self.jobs_for_resource(resource_id)
        active = [x for x in jobs if x['status'] in ('queued','downloading','uploading')]
        return (active[-1] if active else (jobs[-1] if jobs else None))

    def reset_interrupted_downloads(self, now: int | None = None):
        now = int(time.time()) if now is None else int(now)
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            rows = conn.execute("""SELECT resource_id FROM download_jobs
                WHERE status IN ('downloading','uploading')
                  AND (lease_expires_at IS NULL OR lease_expires_at<?)""", (now,)).fetchall()
            result = conn.execute("""UPDATE download_jobs SET status='queued',started_at=NULL,
                local_path=NULL,error_text='requeued after expired lease',lease_token=NULL,
                lease_expires_at=NULL WHERE status IN ('downloading','uploading')
                AND (lease_expires_at IS NULL OR lease_expires_at<?)""", (now,))
            for row in rows:
                conn.execute("UPDATE resources SET cache_status='queued',updated_at=? WHERE resource_id=?",
                             (now, row['resource_id']))
            conn.commit(); return result.rowcount

    def queued_download_jobs(self, limit: int = 10):
        with self._connect() as conn:
            rows = conn.execute("""SELECT * FROM download_jobs WHERE status='queued'
                ORDER BY created_at,job_id LIMIT ?""", (max(1, int(limit)),)).fetchall()
        return [dict(row) for row in rows]

    def get_job(self, job_id: str):
        with self._connect() as conn:
            row = conn.execute('SELECT * FROM download_jobs WHERE job_id=?', (job_id,)).fetchone()
        return dict(row) if row else None

    def claim_download(self, job_id: str, lease_seconds: int = 21600):
        now = int(time.time()); token = uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            result = conn.execute("""UPDATE download_jobs SET status='downloading',started_at=?,
                lease_token=?,lease_expires_at=?,attempt_count=attempt_count+1
                WHERE job_id=? AND status='queued'""",
                (now, token, now + max(60, int(lease_seconds)), job_id))
            if result.rowcount != 1:
                conn.rollback(); return None
            row = conn.execute('SELECT resource_id FROM download_jobs WHERE job_id=?', (job_id,)).fetchone()
            conn.execute("UPDATE resources SET cache_status='downloading',updated_at=? WHERE resource_id=?",
                         (now, row['resource_id']))
            conn.commit()
        return self.get_job(job_id)

    def heartbeat_download(self, job_id: str, lease_token: str, lease_seconds: int = 21600) -> bool:
        now = int(time.time())
        with self._connect() as conn:
            result = conn.execute("""UPDATE download_jobs SET lease_expires_at=?
                WHERE job_id=? AND lease_token=? AND status IN ('downloading','uploading')""",
                (now + max(60, int(lease_seconds)), job_id, lease_token))
            return result.rowcount == 1

    def mark_uploading(self, job_id: str, local_path: str, lease_token: str | None = None):
        now = int(time.time())
        with self._connect() as conn:
            if lease_token is None:
                result = conn.execute("""UPDATE download_jobs SET status='uploading',local_path=?
                    WHERE job_id=? AND status='downloading'""", (local_path, job_id))
            else:
                result = conn.execute("""UPDATE download_jobs SET status='uploading',local_path=?
                    WHERE job_id=? AND status='downloading' AND lease_token=?""",
                    (local_path, job_id, lease_token))
            if result.rowcount != 1:
                raise PaymentMismatch('download job is not owned or downloading')
            conn.execute("UPDATE resources SET cache_status='uploading',updated_at=? WHERE resource_id=(SELECT resource_id FROM download_jobs WHERE job_id=?)",
                         (now, job_id))
        return self.get_job(job_id)

    def complete_download(self, job_id: str, *, storage_chat_id: int, storage_message_id: int,
                          file_size: int, checksum: str, lease_token: str | None = None) -> bool:
        if not storage_chat_id or storage_message_id <= 0 or file_size < 0 or not checksum:
            raise ValueError('invalid completed download')
        now = int(time.time())
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            job = conn.execute('SELECT * FROM download_jobs WHERE job_id=?', (job_id,)).fetchone()
            if not job:
                conn.rollback(); raise PaymentMismatch('download job not found')
            if job['status'] == 'ready':
                conn.rollback(); return False
            if job['status'] != 'uploading' or (lease_token is not None and job['lease_token'] != lease_token):
                conn.rollback(); raise PaymentMismatch('only lease owner of uploading job can complete')
            result = conn.execute("""UPDATE download_jobs SET status='ready',completed_at=?,error_text=NULL,
                lease_token=NULL,lease_expires_at=NULL WHERE job_id=? AND status='uploading'""", (now, job_id))
            if result.rowcount != 1:
                conn.rollback(); raise PaymentMismatch('download state changed concurrently')
            conn.execute("""UPDATE resources SET cache_status='ready',storage_chat_id=?,
                storage_message_id=?,file_size=?,checksum=?,updated_at=? WHERE resource_id=?""",
                (storage_chat_id, storage_message_id, file_size, checksum, now, job['resource_id']))
            conn.commit(); return True

    def fail_download(self, job_id: str, error_text: str, lease_token: str | None = None) -> int:
        now = int(time.time())
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            job = conn.execute('SELECT * FROM download_jobs WHERE job_id=?', (job_id,)).fetchone()
            if not job:
                conn.rollback(); raise PaymentMismatch('download job not found')
            if job['status'] == 'failed':
                conn.rollback(); return 0
            if job['status'] not in ('queued', 'downloading', 'uploading'):
                conn.rollback(); raise PaymentMismatch('job cannot fail from current state')
            if lease_token is not None and job['lease_token'] != lease_token:
                conn.rollback(); raise PaymentMismatch('only lease owner can fail active job')
            error = str(error_text)[:500]
            conn.execute("""UPDATE download_jobs SET status='failed',error_text=?,completed_at=?,
                lease_token=NULL,lease_expires_at=NULL WHERE job_id=?""", (error, now, job_id))
            conn.execute("UPDATE resources SET cache_status='failed',updated_at=? WHERE resource_id=?",
                         (now, job['resource_id']))
            rows = conn.execute("SELECT * FROM download_purchases WHERE resource_id=? AND status='pending'",
                                (job['resource_id'],)).fetchall()
            refunded = 0
            for row in rows:
                conn.execute("""INSERT OR IGNORE INTO balance_ledger
                    (tg_user_id,order_id,kind,delta_units,created_at) VALUES(?,?,?,?,?)""",
                    (row['tg_user_id'], row['purchase_id'], 'refund', row['price_units'], now))
                if conn.execute('SELECT changes()').fetchone()[0]:
                    conn.execute('UPDATE users SET balance_units=balance_units+? WHERE tg_user_id=?',
                                 (row['price_units'], row['tg_user_id']))
                    refunded += 1
                conn.execute("""UPDATE download_purchases SET status='refunded',refunded_at=?,error_text=?
                    WHERE purchase_id=? AND status='pending'""", (now, error, row['purchase_id']))
            conn.commit(); return refunded

    def get_purchase(self, purchase_id: str):
        with self._connect() as conn:
            row = conn.execute('SELECT * FROM download_purchases WHERE purchase_id=?',
                               (purchase_id,)).fetchone()
        return dict(row) if row else None

    def get_purchase_for(self, tg_user_id: int, resource_id: str):
        with self._connect() as conn:
            row = conn.execute('''SELECT * FROM download_purchases
                WHERE tg_user_id=? AND resource_id=? ORDER BY created_at DESC,purchase_id DESC LIMIT 1''',
                (tg_user_id, resource_id)).fetchone()
        return dict(row) if row else None

    def purchases_for(self, tg_user_id: int):
        with self._connect() as conn:
            rows = conn.execute('''SELECT * FROM download_purchases WHERE tg_user_id=?
                                   ORDER BY created_at,purchase_id''', (tg_user_id,)).fetchall()
        return [dict(row) for row in rows]

    def pending_purchases_for_resource(self, resource_id: str):
        with self._connect() as conn:
            rows = conn.execute('''SELECT * FROM download_purchases
                WHERE resource_id=? AND status='pending' ORDER BY created_at,purchase_id''',
                (resource_id,)).fetchall()
        return [dict(row) for row in rows]

    def ready_pending_purchases(self, limit: int = 100):
        with self._connect() as conn:
            rows = conn.execute("""SELECT p.* FROM download_purchases p
                JOIN resources r ON r.resource_id=p.resource_id
                WHERE p.status='pending' AND r.cache_status='ready'
                  AND r.storage_chat_id IS NOT NULL AND r.storage_message_id IS NOT NULL
                ORDER BY p.created_at,p.purchase_id LIMIT ?""",
                (min(max(int(limit), 1), 1000),)).fetchall()
        return [dict(row) for row in rows]

    def reset_interrupted_deliveries(self):
        with self._connect() as conn:
            result = conn.execute("""UPDATE download_purchases
                SET status='manual_review',error_text='delivery result unknown after restart'
                WHERE status='delivering'""")
            return result.rowcount

    def claim_delivery(self, purchase_id: str) -> bool:
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT status FROM download_purchases WHERE purchase_id=?',
                               (purchase_id,)).fetchone()
            if not row:
                conn.rollback(); raise PaymentMismatch('purchase not found')
            if row['status'] != 'pending':
                conn.rollback(); return False
            conn.execute("UPDATE download_purchases SET status='delivering' WHERE purchase_id=?",
                         (purchase_id,))
            conn.commit(); return True

    def mark_delivered(self, purchase_id: str, telegram_message_id: int) -> bool:
        now = int(time.time())
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM download_purchases WHERE purchase_id=?',
                               (purchase_id,)).fetchone()
            if not row:
                conn.rollback(); raise PaymentMismatch('purchase not found')
            if row['status'] == 'delivered':
                conn.rollback()
                if int(row['telegram_message_id']) != int(telegram_message_id):
                    raise PaymentMismatch('purchase already delivered with another message')
                return False
            if row['status'] != 'delivering':
                conn.rollback(); raise PaymentMismatch('purchase is not delivering')
            conn.execute('''UPDATE download_purchases SET status='delivered',telegram_message_id=?,
                            delivered_at=?,error_text=NULL WHERE purchase_id=?''',
                         (telegram_message_id, now, purchase_id))
            conn.commit(); return True

    def mark_delivery_unknown(self, purchase_id: str, error_text: str) -> bool:
        with self._connect() as conn:
            result = conn.execute("""UPDATE download_purchases
                SET status='manual_review',error_text=? WHERE purchase_id=? AND status='delivering'""",
                (str(error_text)[:500], purchase_id))
            return result.rowcount == 1

    def fail_delivery(self, purchase_id: str, error_text: str) -> bool:
        now = int(time.time())
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM download_purchases WHERE purchase_id=?',
                               (purchase_id,)).fetchone()
            if not row:
                conn.rollback(); raise PaymentMismatch('purchase not found')
            if row['status'] == 'refunded':
                conn.rollback(); return False
            if row['status'] != 'delivering':
                conn.rollback(); raise PaymentMismatch('purchase is not delivering')
            conn.execute("""INSERT INTO balance_ledger
                (tg_user_id,order_id,kind,delta_units,created_at) VALUES(?,?,?,?,?)""",
                (row['tg_user_id'], purchase_id, 'refund', row['price_units'], now))
            conn.execute('UPDATE users SET balance_units=balance_units+? WHERE tg_user_id=?',
                         (row['price_units'], row['tg_user_id']))
            conn.execute("""UPDATE download_purchases SET status='refunded',refunded_at=?,error_text=?
                            WHERE purchase_id=? AND status='delivering'""",
                         (now, str(error_text)[:500], purchase_id))
            conn.commit(); return True

    def resolve_manual_delivery(self, purchase_id: str, telegram_message_id: int) -> bool:
        if int(telegram_message_id) <= 0:
            raise ValueError('invalid telegram message id')
        with self._connect() as conn:
            result = conn.execute("""UPDATE download_purchases SET status='delivered',
                telegram_message_id=?,delivered_at=?,error_text=NULL
                WHERE purchase_id=? AND status='manual_review'""",
                (int(telegram_message_id), int(time.time()), purchase_id))
            if result.rowcount == 1: return True
            row = conn.execute('SELECT status FROM download_purchases WHERE purchase_id=?',
                               (purchase_id,)).fetchone()
            if not row: raise PaymentMismatch('purchase not found')
            if row['status'] == 'delivered': return False
            raise PaymentMismatch('purchase is not awaiting manual review')

    def refund_purchase(self, purchase_id: str, error_text: str) -> bool:
        now = int(time.time())
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM download_purchases WHERE purchase_id=?',
                               (purchase_id,)).fetchone()
            if not row:
                conn.rollback(); raise PaymentMismatch('purchase not found')
            if row['status'] == 'refunded':
                conn.rollback(); return False
            if row['status'] not in ('pending', 'manual_review'):
                conn.rollback(); raise PaymentMismatch('only pending or reviewed purchases can be refunded')
            conn.execute('''INSERT INTO balance_ledger
                (tg_user_id,order_id,kind,delta_units,created_at) VALUES(?,?,?,?,?)''',
                (row['tg_user_id'], purchase_id, 'refund', row['price_units'], now))
            conn.execute('UPDATE users SET balance_units=balance_units+? WHERE tg_user_id=?',
                         (row['price_units'], row['tg_user_id']))
            conn.execute('''UPDATE download_purchases SET status='refunded',refunded_at=?,error_text=?
                            WHERE purchase_id=?''', (now, str(error_text)[:500], purchase_id))
            conn.commit(); return True

    def set_resource_price(self, resource_id: str, price_units: int):
        price_units = int(price_units)
        if price_units <= 0 or price_units > 1000000000000:
            raise ValueError('invalid resource price')
        with self._connect() as conn:
            result = conn.execute('UPDATE resources SET price_units=?,updated_at=? WHERE resource_id=?',
                                  (price_units, int(time.time()), resource_id))
            if result.rowcount != 1:
                raise ValueError('resource not found')
        return self.get_resource(resource_id)

    def admin_overview(self):
        with self._connect() as conn:
            return {
                'users': conn.execute('SELECT COUNT(*) FROM users').fetchone()[0],
                'resources': conn.execute('SELECT COUNT(*) FROM resources').fetchone()[0],
                'pending_purchases': conn.execute("SELECT COUNT(*) FROM download_purchases WHERE status='pending'").fetchone()[0],
                'manual_review_purchases': conn.execute("SELECT COUNT(*) FROM download_purchases WHERE status='manual_review'").fetchone()[0],
                'delivered_purchases': conn.execute("SELECT COUNT(*) FROM download_purchases WHERE status='delivered'").fetchone()[0],
                'refunded_purchases': conn.execute("SELECT COUNT(*) FROM download_purchases WHERE status='refunded'").fetchone()[0],
                'queued_jobs': conn.execute("SELECT COUNT(*) FROM download_jobs WHERE status='queued'").fetchone()[0],
                'active_jobs': conn.execute("SELECT COUNT(*) FROM download_jobs WHERE status IN ('downloading','uploading')").fetchone()[0],
            }

    def admin_list(self, kind: str, limit: int = 200):
        limit = min(max(int(limit), 1), 1000)
        queries = {
            'users': 'SELECT tg_user_id,balance_units,created_at FROM users ORDER BY created_at DESC LIMIT ?',
            'resources': 'SELECT * FROM resources ORDER BY updated_at DESC LIMIT ?',
            'jobs': 'SELECT * FROM download_jobs ORDER BY created_at DESC LIMIT ?',
            'purchases': 'SELECT * FROM download_purchases ORDER BY created_at DESC LIMIT ?',
        }
        if kind not in queries:
            raise ValueError('unknown admin list')
        with self._connect() as conn:
            rows = conn.execute(queries[kind], (limit,)).fetchall()
        return [dict(row) for row in rows]

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
