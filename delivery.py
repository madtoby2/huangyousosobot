#!/usr/bin/env python3
"""Concurrency-safe delivery from channel cache to a paid buyer."""
from __future__ import annotations

from telegram.error import BadRequest, Forbidden


class DeliveryFailed(RuntimeError): pass
class DeliveryNotReady(DeliveryFailed): pass


async def deliver_purchase(store, bot, purchase_id: str):
    purchase = store.get_purchase(purchase_id)
    if not purchase: raise DeliveryFailed('purchase not found')
    if purchase['status'] == 'delivered':
        return {'delivered_now': False, 'telegram_message_id': purchase['telegram_message_id']}
    if purchase['status'] == 'refunded':
        raise DeliveryFailed(purchase.get('error_text') or 'purchase already refunded')
    if purchase['status'] in ('delivering', 'manual_review'):
        return {'delivered_now': False, 'in_progress': purchase['status'] == 'delivering',
                'manual_review': purchase['status'] == 'manual_review',
                'telegram_message_id': purchase.get('telegram_message_id')}
    if purchase['status'] != 'pending': raise DeliveryFailed('purchase is not deliverable')

    resource = store.get_resource(purchase['resource_id'])
    if (not resource or resource['cache_status'] != 'ready' or
            not resource['storage_chat_id'] or not resource['storage_message_id']):
        raise DeliveryNotReady('download is not ready')
    if not store.claim_delivery(purchase_id):
        current = store.get_purchase(purchase_id)
        return {'delivered_now': False,
                'in_progress': current['status'] == 'delivering',
                'manual_review': current['status'] == 'manual_review',
                'telegram_message_id': current.get('telegram_message_id')}

    try:
        copied = await bot.copy_message(chat_id=purchase['tg_user_id'],
                                        from_chat_id=resource['storage_chat_id'],
                                        message_id=resource['storage_message_id'])
    except (BadRequest, Forbidden) as exc:
        store.fail_delivery(purchase_id, str(exc))
        raise DeliveryFailed(str(exc)) from exc
    except Exception as exc:
        # A timeout/disconnect can happen after Telegram accepted the copy. Never auto-refund.
        store.mark_delivery_unknown(purchase_id, 'telegram result unknown: ' + str(exc))
        raise DeliveryFailed(str(exc)) from exc

    try:
        store.mark_delivered(purchase_id, copied.message_id)
    except Exception as exc:
        # The file was definitely copied; a DB failure must not turn it into a free order.
        store.mark_delivery_unknown(purchase_id, 'copied but persistence failed: ' + str(exc))
        raise DeliveryFailed(str(exc)) from exc
    return {'delivered_now': True, 'telegram_message_id': copied.message_id}
