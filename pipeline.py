#!/usr/bin/env python3
"""One durable on-demand job: isolated download, upload, then fan-out."""
from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from pathlib import Path

from delivery import DeliveryFailed, deliver_purchase


def cleanup_stale_job_dirs(work_dir, *, older_than_seconds=86400, now=None):
    root = Path(work_dir)
    if not root.exists(): return 0
    cutoff = (time.time() if now is None else float(now)) - max(60, int(older_than_seconds))
    removed = 0
    for entry in root.glob('job-*'):
        try:
            if entry.is_dir() and entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry); removed += 1
        except FileNotFoundError:
            pass
    return removed


async def process_download_job(store, job_id, download_callable, uploader, bot, work_dir,
                               progress=None, stage_callback=None, prepare_callable=None):
    job = store.claim_download(job_id)
    if not job:
        current = store.get_job(job_id)
        return {'status': current['status'] if current else 'missing', 'delivered': 0, 'refunded': 0}
    resource = store.get_resource(job['resource_id'])
    lease_token = job.get('lease_token')

    async def stage(name, data=None):
        if stage_callback:
            try: await stage_callback(name, data)
            except Exception: pass

    root = Path(work_dir); root.mkdir(parents=True, exist_ok=True)
    job_dir = tempfile.mkdtemp(prefix=f'job-{job_id}-', dir=str(root))
    await stage('downloading')
    cache_ready = False
    try:
        def heartbeat_progress(written, expected):
            store.heartbeat_download(job_id, lease_token)
            if progress: progress(written, expected)
        result = await asyncio.to_thread(download_callable, resource['download_url'], job_dir,
                                         progress=heartbeat_progress)
        if prepare_callable:
            await stage('preparing', result)
            prepared = await asyncio.to_thread(
                prepare_callable, result['path'], resource['source'])
            result = {**result, **prepared}
        local_path = result['path']
        store.mark_uploading(job_id, local_path, lease_token)
        await stage('uploading', result)
        uploaded = await uploader.upload(local_path, resource['title'])
        store.complete_download(job_id, storage_chat_id=uploaded['storage_chat_id'],
                                storage_message_id=uploaded['storage_message_id'],
                                file_size=result['file_size'], checksum=result['checksum'], lease_token=lease_token)
        cache_ready = True
        await stage('delivering')
    except Exception as exc:
        refunded = store.fail_download(job_id, str(exc), lease_token)
        await stage('failed', {'error': str(exc), 'refunded': refunded})
        return {'status': 'failed', 'delivered': 0, 'refunded': refunded, 'error': str(exc)}
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)

    delivered = failed = refunded = manual_review = 0
    if cache_ready:
        for purchase in store.pending_purchases_for_resource(resource['resource_id']):
            try:
                outcome = await deliver_purchase(store, bot, purchase['purchase_id'])
                delivered += int(bool(outcome['delivered_now']))
            except DeliveryFailed:
                failed += 1
                current = store.get_purchase(purchase['purchase_id'])
                refunded += int(current['status'] == 'refunded')
                manual_review += int(current['status'] == 'manual_review')
    await stage('manual_review' if manual_review else 'ready',
                {'delivered': delivered, 'delivery_failed': failed,
                 'refunded': refunded, 'manual_review': manual_review})
    return {'status': 'ready', 'delivered': delivered, 'delivery_failed': failed,
            'refunded': refunded, 'manual_review': manual_review}
