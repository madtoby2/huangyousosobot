#!/usr/bin/env python3
"""黄油搜搜 bot - 双域搜索主程序（文字列表 + 详情带图）

域1: 🕹️ 黄油搜索 (Ryuugames + Otomi) -> 列表 -> 详情带封面+简介+下载按钮
域2: 🔍 BT 搜索 (sukebei + javdb) -> 列表 -> 详情磁力链接
"""

import os
import io
import asyncio
import logging
import html as h
import re
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, CopyTextButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from curl_cffi import requests as cffi

load_dotenv(Path(__file__).parent / '.env')

TOKEN = os.environ.get('BOT_TOKEN', '')
if not TOKEN:
    raise SystemExit('BOT_TOKEN not set')

import search_ryuugames
import search_otomi
import search_bt
import translate
from okaypay import OkayPayClient, OkayPayError
from wallet_store import (InsufficientBalance, WalletStore, PaymentMismatch)
from archive_processor import passwords_for_source, prepare_archive
from artifacts import game_resource_id
from delivery import DeliveryFailed, deliver_purchase
from downloader import download_game_url
from pipeline import cleanup_stale_job_dirs, process_download_job
from uploader import UploaderUnavailable, build_telethon_uploader

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
logger = logging.getLogger('searchbot')
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)

OKPAY_SHOP_ID = os.environ.get('OKPAY_SHOP_ID', '')
OKPAY_API_KEY = os.environ.get('OKPAY_API_KEY', '')
PUBLIC_BASE_URL = os.environ.get('PUBLIC_BASE_URL', '').rstrip('/')
WALLET_DB = os.environ.get('WALLET_DB', str(Path(__file__).parent / 'wallet.sqlite3'))
_wallet_store = WalletStore(WALLET_DB)
_user_state = {}


def _state(user_id):
    return _user_state.setdefault(user_id, {})


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _state(update.effective_user.id).clear()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton('🎮 黄油搜索', callback_data='domain_ryu')],
        [InlineKeyboardButton('🔍 BT搜索', callback_data='domain_bt')],
        [InlineKeyboardButton('💰 我的钱包', callback_data='wallet_home')],
    ])
    await update.message.reply_text(
        '👋 欢迎！选择搜索类型：\n\n'
        '🎮 <b>黄油搜索</b> - 搜成人游戏 (Ryuugames/Otomi)\n'
        '🔍 <b>BT搜索</b> - 搜 BT 磁力资源 (Sukebei/JavDB)\n\n'
        '输入关键词开始搜索，或点下面按钮切换搜索域~',
        parse_mode='HTML',
        reply_markup=kb,
    )


async def domain_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id
    domain = q.data.split('_')[1]
    st = _state(user_id)
    st['domain'] = domain
    st.pop('results', None)
    st.pop('page', None)
    name = '🎮 黄油搜索' if domain == 'ryu' else '🔍 BT搜索'
    await q.edit_message_text(
        f'已切换：<b>{name}</b>\n\n直接输入关键词开始搜索~',
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('↩️ 返回', callback_data='back_start')]]),
    )


async def back_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _state(q.from_user.id).clear()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton('🎮 黄油搜索', callback_data='domain_ryu')],
        [InlineKeyboardButton('🔍 BT搜索', callback_data='domain_bt')],
        [InlineKeyboardButton('💰 我的钱包', callback_data='wallet_home')],
    ])
    await q.edit_message_text(
        '👋 选择搜索类型：\n\n'
        '🎮 <b>黄油搜索</b> - 搜成人游戏 (Ryuugames/Otomi)\n'
        '🔍 <b>BT搜索</b> - 搜 BT 磁力资源 (Sukebei/JavDB)',
        parse_mode='HTML',
        reply_markup=kb,
    )


def _prepare_paid_archive(path: str, source: str, title: str):
    return prepare_archive(path, passwords_for_source(source), output_name=title)


def _delivery_configured():
    channel = os.environ.get('STORAGE_CHANNEL_ID', '').strip()
    session_path = Path(os.environ.get(
        'UPLOADER_SESSION', str(Path(__file__).parent / 'uploader_351961666576.session')))
    auth_path = Path(__file__).parent / '.uploader_auth.json'
    if not channel or not session_path.exists() or not auth_path.exists():
        return False
    try:
        import json
        return bool(json.loads(auth_path.read_text()).get('authorized'))
    except Exception:
        return False


async def buy_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id
    if not _delivery_configured():
        await q.message.reply_text('⚠️ 付费下载暂未就绪，不会扣款。')
        return
    resource_id = q.data.split('_', 1)[1]
    offer = _state(user_id).get('paid_offers', {}).get(resource_id)
    if not offer:
        await q.message.reply_text('❌ 下载信息已过期，请重新打开游戏详情。')
        return
    try:
        purchase, charged, job_created = await asyncio.to_thread(
            _wallet_store.create_download_purchase, user_id, offer)
    except InsufficientBalance:
        await q.message.reply_text(
            f"❌ 余额不足，需要 {format_balance(offer['price_units'])} USDT。",
            reply_markup=_wallet_keyboard())
        return
    resource = _wallet_store.get_resource(resource_id)
    if resource and resource['cache_status'] == 'ready':
        try:
            outcome = await deliver_purchase(_wallet_store, context.application.bot,
                                             purchase['purchase_id'])
        except DeliveryFailed:
            await q.message.reply_text('❌ 文件交付失败，已自动退款。')
            return
        if outcome['delivered_now']:
            await q.message.reply_text(
                f"✅ 文件发送成功，余额 {format_balance(_wallet_store.get_balance_units(user_id))} USDT。")
        else:
            await q.message.reply_text('✅ 该资源此前已经交付。')
        return
    if charged:
        state = '已创建下载队列任务' if job_created else '已加入现有下载队列任务'
        await q.message.reply_text(
            f"✅ 已扣款 {format_balance(offer['price_units'])} USDT，{state}。\n"
            '⏳ 文件下载、上传完成后会自动发送；失败将自动退款。')
    else:
        await q.message.reply_text('⏳ 该资源已有下载队列任务，不会重复扣款。')


def _payment_client():
    if not OKPAY_SHOP_ID or not OKPAY_API_KEY:
        raise OkayPayError('支付通道尚未配置')
    return OkayPayClient(OKPAY_SHOP_ID, OKPAY_API_KEY)


TOPUP_PROMPT = (
    '➕ <b>充值 USDT</b>\n\n'
    '充值范围：1–10000 USDT\n'
    '支持最多 8 位小数（例如：1.12345678）\n\n'
    '请输入充值金额：'
)
TOPUP_INVALID = '❌ 金额无效。充值范围：1–10000 USDT，支持最多 8 位小数。'


def _parse_topup_amount(raw: str):
    value = (raw or '').strip()
    if not re.fullmatch(r'[0-9]{1,5}(?:\.[0-9]{1,8})?', value):
        return None
    amount = Decimal(value)
    if amount < Decimal('1') or amount > Decimal('10000'):
        return None
    return value


def format_balance(units: int) -> str:
    value = f'{Decimal(units) / Decimal(100000000):.8f}'
    return value.rstrip('0').rstrip('.') or '0'


def _select_paid_offer(detail, price_units, store=None):
    priorities = ('mediafire.com', 'pixeldrain.com')
    selected = None
    for wanted in priorities:
        for button in detail.get('download_buttons', []):
            host = (urlsplit(button.get('url', '')).hostname or '').lower()
            if host == wanted or host.endswith('.' + wanted):
                selected = button
                break
        if selected:
            break
    if not selected:
        return None
    source = detail.get('source', '')
    source_url = detail.get('url', '')
    try:
        version = detail.get('version') or detail.get('info_title') or 'unknown'
        resource_id = game_resource_id(source, source_url, version, selected['url'])
    except ValueError:
        return None
    if store:
        existing = store.get_resource(resource_id)
        if existing:
            price_units = existing['price_units']
    return {
        'resource_id': resource_id,
        'title': detail.get('title') or 'Untitled game',
        'source': source,
        'source_url': source_url,
        'download_url': selected['url'],
        'version': version,
        'price_units': int(price_units),
    }


def _paid_download_button(offer):
    return InlineKeyboardButton(
        f"⚡ {format_balance(offer['price_units'])} USDT · 付费下载",
        callback_data=f"buy_{offer['resource_id']}",
    )


def _wallet_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('➕ 充值 USDT', callback_data='wallet_topup')],
        [InlineKeyboardButton('↩️ 返回首页', callback_data='back_start')],
    ])


def _create_topup_checkout(tg_user_id, amount, store, client, public_base_url):
    if not public_base_url.startswith('https://'):
        raise ValueError('PUBLIC_BASE_URL must use HTTPS')
    order = store.create_topup(tg_user_id, amount, 'USDT')
    checkout = client.create_payment(
        order['order_id'], amount, 'USDT',
        f"{public_base_url.rstrip('/')}/api/okpay/notify",
        'SearchBot balance top-up',
    )
    return store.attach_provider(order['order_id'], checkout['provider_order_id'],
                                 checkout['payment_url'])


async def wallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = (f'💰 <b>我的钱包</b>\n\n可用余额：'
            f'<b>{format_balance(_wallet_store.get_balance_units(user_id))} USDT</b>\n\n'
            '充值到账后可用于付费资源。')
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, parse_mode='HTML',
                                                       reply_markup=_wallet_keyboard())
    else:
        await update.message.reply_text(text, parse_mode='HTML', reply_markup=_wallet_keyboard())


async def wallet_topup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _state(q.from_user.id)['awaiting_topup'] = True
    await q.edit_message_text(
        TOPUP_PROMPT,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('取消', callback_data='wallet_home')]]),
    )


async def topup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Create a USDT checkout directly with /topup AMOUNT or prompt for it."""
    raw = ' '.join(getattr(context, 'args', None) or []).strip()
    if raw:
        amount = _parse_topup_amount(raw)
        if amount is None:
            await update.message.reply_text(
                TOPUP_INVALID)
            return
        await _handle_topup_message(update, context, amount)
        return
    _state(update.effective_user.id)['awaiting_topup'] = True
    await update.message.reply_text(
        TOPUP_PROMPT,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('取消', callback_data='wallet_home')]]),
    )


async def _handle_topup_message(update, context, amount):
    st = _state(update.effective_user.id)
    st.pop('awaiting_topup', None)
    status = await update.message.reply_text('⏳ 正在创建充值订单…')
    try:
        order = await asyncio.to_thread(
            _create_topup_checkout, update.effective_user.id, amount,
            _wallet_store, _payment_client(), PUBLIC_BASE_URL,
        )
    except Exception as exc:
        logger.exception('创建充值订单失败')
        await status.edit_text(f'❌ 创建支付订单失败：{h.escape(str(exc))}', parse_mode='HTML')
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton('💳 前往 OKPay 支付', url=order['payment_url'])],
        [InlineKeyboardButton('🔄 我已支付，立即查单', callback_data=f"checkpay_{order['order_id']}")],
        [InlineKeyboardButton('💰 返回钱包', callback_data='wallet_home')],
    ])
    await status.edit_text(
        f'✅ 充值订单已创建\n\n金额：<b>{h.escape(amount)} USDT</b>\n'
        '有效期：30 分钟\n\n支付完成后系统会自动到账，也可点击下方按钮立即查单。',
        parse_mode='HTML', reply_markup=kb,
    )


def _verify_topup_order(tg_user_id, order_id, store, client):
    order = store.get_order(order_id)
    if not order or order['tg_user_id'] != tg_user_id:
        raise PaymentMismatch('订单不存在或不属于当前用户')
    if order['status'] == 'paid':
        return order, False
    payment = client.check_payment(order['provider_order_id'])
    if not payment:
        return order, None
    credited = store.credit_verified(payment)
    return store.get_order(order_id), credited


async def check_topup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer('正在查单…')
    order_id = q.data.split('_', 1)[1]
    try:
        order, credited = await asyncio.to_thread(
            _verify_topup_order, q.from_user.id, order_id, _wallet_store, _payment_client())
    except Exception as exc:
        logger.exception('主动查单失败')
        await q.answer(f'查单失败：{str(exc)[:80]}', show_alert=True)
        return
    if credited is None:
        await q.answer('暂未检测到付款，请稍后重试。', show_alert=True)
        return
    await q.edit_message_text(
        f'✅ 已到账：<b>{h.escape(order["amount_text"])} {order["asset"]}</b>\n'
        f'当前余额：<b>{format_balance(_wallet_store.get_balance_units(q.from_user.id))} USDT</b>',
        parse_mode='HTML', reply_markup=_wallet_keyboard())


async def _poll_payments(application):
    while True:
        try:
            client = _payment_client()
            for order in await asyncio.to_thread(_wallet_store.pending_orders):
                try:
                    payment = await asyncio.to_thread(client.check_payment, order['provider_order_id'])
                    if payment and await asyncio.to_thread(_wallet_store.credit_verified, payment):
                        await application.bot.send_message(
                            order['tg_user_id'],
                            f"✅ 充值成功：{order['amount_text']} {order['asset']}\n发送 /wallet 查看余额。")
                except Exception:
                    logger.exception('轮询充值订单失败 order=%s', order['order_id'])
        except Exception:
            logger.exception('支付轮询器异常')
        await asyncio.sleep(30)


async def _download_worker(application):
    try:
        uploader = build_telethon_uploader()
    except UploaderUnavailable:
        logger.exception('付费下载上传器未就绪')
        return
    work_dir = os.environ.get('DOWNLOAD_DIR', str(Path(__file__).parent / 'downloads'))
    await asyncio.to_thread(cleanup_stale_job_dirs, work_dir)
    while True:
        try:
            await asyncio.to_thread(_wallet_store.reset_interrupted_downloads)
            for pending in await asyncio.to_thread(_wallet_store.ready_pending_purchases, 20):
                try:
                    await deliver_purchase(_wallet_store, application.bot, pending['purchase_id'])
                except DeliveryFailed:
                    logger.exception('缓存资源交付失败 purchase=%s', pending['purchase_id'])
            jobs = await asyncio.to_thread(_wallet_store.queued_download_jobs, 1)
            if not jobs:
                await asyncio.sleep(5)
                continue
            job = jobs[0]
            waiting = await asyncio.to_thread(
                _wallet_store.pending_purchases_for_resource, job['resource_id'])
            status_messages = {}
            for purchase in waiting:
                try:
                    status_messages[purchase['tg_user_id']] = await application.bot.send_message(
                        purchase['tg_user_id'], '⏬ 付费资源开始下载…')
                except Exception:
                    logger.exception('下载进度通知发送失败 user=%s', purchase['tg_user_id'])

            async def update_stage(stage, data=None):
                texts = {
                    'downloading': '⏬ 正在下载付费资源…',
                    'preparing': '🔓 下载完成，正在解密并重包为无密码 ZIP…',
                    'uploading': '☁️ 解密重包完成，正在上传 Telegram…',
                    'delivering': '📦 上传完成，正在发送文件…',
                    'ready': '✅ 文件交付流程已完成。',
                    'manual_review': '⚠️ Telegram 返回结果不确定，订单已转人工核对，不会重复扣款或自动退款。',
                    'failed': '❌ 文件下载或上传失败，费用已自动退款。',
                }
                text = texts.get(stage)
                if not text:
                    return
                for user_id, message in status_messages.items():
                    try:
                        await message.edit_text(text)
                    except Exception:
                        logger.exception('进度更新失败 user=%s stage=%s', user_id, stage)

            await process_download_job(
                _wallet_store, job['job_id'], download_game_url, uploader,
                application.bot, work_dir, stage_callback=update_stage,
                prepare_callable=_prepare_paid_archive)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('付费下载 worker 异常')
            await asyncio.sleep(10)


async def _post_init(application):
    if OKPAY_SHOP_ID and OKPAY_API_KEY:
        application.bot_data['payment_poll_task'] = asyncio.create_task(_poll_payments(application))
    if _delivery_configured():
        await asyncio.to_thread(_wallet_store.reset_interrupted_downloads)
        await asyncio.to_thread(_wallet_store.reset_interrupted_deliveries)
        application.bot_data['download_task'] = asyncio.create_task(_download_worker(application))


async def _post_shutdown(application):
    for key in ('payment_poll_task', 'download_task'):
        task = application.bot_data.get(key)
        if task:
            task.cancel()


async def do_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """文本消息 -> 搜索"""
    user_id = update.effective_user.id
    keyword = (update.message.text or '').strip()
    if not keyword or keyword.startswith('/'):
        return

    st = _state(user_id)
    if st.get('awaiting_topup'):
        amount = _parse_topup_amount(keyword)
        if amount is None:
            await update.message.reply_text(TOPUP_INVALID)
            return
        await _handle_topup_message(update, context, amount)
        return
    domain = st.get('domain', 'ryu')

    status = await update.message.reply_text('🔎 搜索中…')

    try:
        if domain == 'ryu':
            res = await asyncio.to_thread(combined_game_search, keyword)
        else:
            res = await asyncio.to_thread(search_bt.search, keyword, 10)
    except Exception as e:
        await status.edit_text(f'❌ 搜索出错: {e}')
        return

    results = res.get('results', [])
    if not results:
        await status.edit_text(f'😔 没找到 "{keyword}" 的结果，换个关键词试试~')
        return

    st['results'] = results
    st['page'] = 0
    st['keyword'] = keyword
    await _render_page(update, status, st)


def _title_matches(item, keyword: str) -> bool:
    """标题是否匹配搜索词（大小写不敏感）"""
    title = (item.get('title') or '').lower()
    kw = keyword.lower().strip()
    if not kw:
        return True
    # 番号/RJ 精确匹配
    if kw in title:
        return True
    # 空格分词：任意一个词命中标题即可
    words = [w for w in kw.split() if len(w) >= 2]
    if words and any(w in title for w in words):
        return True
    # 单个短词（如 lala）子串
    return kw in title


def combined_game_search(keyword: str, limit: int = 10):
    """黄油搜索：Ryuugames + Otomi 聚合 + 标题相关度过滤"""
    r1 = search_ryuugames.search(keyword, limit)
    r2 = search_otomi.search(keyword, limit)
    results = (r1.get('results', []) + r2.get('results', []))[:limit]
    # 标题相关度过滤：保留标题含关键词的结果；全不匹配才回退全部
    matched = [x for x in results if _title_matches(x, keyword)]
    if matched:
        results = matched
    return {'results': results}


def _build_bt_detail(detail):
    """构建 BT 详情：磁力使用 Telegram 原生复制按钮，不能作为 URL。"""
    lines = [f"🔗 <b>{h.escape(detail.get('title') or '')}</b>"]
    if detail.get('seeders'):
        lines.append(f"🌱 做种: {detail['seeders']}")
    if detail.get('source_label'):
        lines.append(f"📡 {detail['source_label']}")
    if detail.get('code'):
        lines.append(f"🎬 番号: {h.escape(detail['code'])}")
    if detail.get('release_date'):
        lines.append(f"📅 发行: {h.escape(detail['release_date'])}")

    btns = []
    magnet = detail.get('magnet')
    if magnet:
        # Telegram CopyTextButton 上限 256 字符；xt 哈希本身即可由 BT 客户端解析。
        copyable_magnet = magnet.split('&', 1)[0]
        btns.append([InlineKeyboardButton(
            '🧲 复制磁力链接',
            copy_text=CopyTextButton(text=copyable_magnet),
        )])
    if detail.get('url'):
        btns.append([InlineKeyboardButton('🌐 BT 原站', url=detail['url'])])
    if detail.get('metadata_url'):
        btns.append([InlineKeyboardButton('🎬 作品资料', url=detail['metadata_url'])])
    btns.append([InlineKeyboardButton('↩️ 返回列表', callback_data='page_refresh')])
    return '\n'.join(lines), InlineKeyboardMarkup(btns)


def _download_image(url: str, timeout: int = 15) -> bytes | None:
    """下载图片为 bytes（curl_cffi 指纹，兼容 CDN）"""
    try:
        s = cffi.Session(impersonate='chrome')
        s.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'})
        r = s.get(url, timeout=timeout)
        if r.status_code == 200 and r.content and len(r.content) > 1000:
            return r.content
    except Exception:
        pass
    return None


async def _render_page(update, status_msg, st):
    """渲染当前页结果（纯文字列表，点击下钻详情带图）"""
    results = st.get('results', [])
    page = st.get('page', 0)
    domain = st.get('domain', 'ryu')
    kw = st.get('keyword', '')

    per_page = 8
    start = page * per_page
    end = min(start + per_page, len(results))
    page_items = results[start:end]
    total_pages = (len(results) + per_page - 1) // per_page

    lines = [f'🔎 <b>"{h.escape(kw)}"</b> 搜索结果 ({len(results)} 条) - 第{page + 1}/{total_pages}页\n']
    btns = []

    for i, item in enumerate(page_items):
        abs_idx = start + i
        title = (item.get('title') or '?')[:60]
        src = item.get('source_label', '')
        lines.append(f'<b>{abs_idx + 1}.</b> {h.escape(title)}')
        if 'seeders' in item:
            lines.append(f'      🌱{item.get("seeders", "?")} ｜ {src}')
        else:
            lines.append(f'      {src}')
        btns.append([InlineKeyboardButton(f'{abs_idx + 1}. {title}', callback_data=f'pick_{abs_idx}')])

    # pagination row
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton('⬅️ 上一页', callback_data='page_prev'))
    nav.append(InlineKeyboardButton(f'{page + 1}/{total_pages}', callback_data='noop'))
    if end < len(results):
        nav.append(InlineKeyboardButton('➡️ 下一页', callback_data='page_next'))
    if nav:
        btns.append(nav)
    btns.append([InlineKeyboardButton('↩️ 返回', callback_data='back_start')])

    text = '\n'.join(lines)
    keyboard = InlineKeyboardMarkup(btns)
    # Detail cards can be photos. Telegram cannot edit a photo message into a
    # text-only result list, so send the list first and then remove the card.
    if not getattr(status_msg, 'text', None):
        replacement = await status_msg.reply_text(
            text, parse_mode='HTML', reply_markup=keyboard)
        await status_msg.delete()
        return replacement
    try:
        await status_msg.edit_text(text, parse_mode='HTML', reply_markup=keyboard)
    except Exception:
        await status_msg.edit_text(text[:300], parse_mode='HTML', reply_markup=keyboard)
    return status_msg


async def page_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id
    st = _state(user_id)
    if q.data == 'page_prev':
        st['page'] = max(0, st.get('page', 0) - 1)
    elif q.data == 'page_next':
        st['page'] = st.get('page', 0) + 1
    await _render_page(update, q.message, st)


async def pick_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """选择某个结果 -> 拉详情（带封面图）"""
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id
    st = _state(user_id)
    results = st.get('results', [])
    idx = int(q.data.split('_')[1])
    if idx >= len(results):
        await q.edit_message_text('❌ 结果已过期，重新搜索~')
        return
    item = results[idx]

    await q.edit_message_text(f'⏳ 正在获取 {h.escape(item.get("title", "")[:40])} 详情…')

    try:
        if st.get('domain') == 'ryu':
            src_mod = search_ryuugames if item.get('source') == 'ryuugames' else search_otomi
            detail = await asyncio.to_thread(src_mod.get_detail, item['url'])
        else:
            detail = await asyncio.to_thread(search_bt.enrich_bt_result, item)
    except Exception as e:
        await q.edit_message_text(f'❌ 详情获取失败: {e}')
        return

    if 'error' in detail:
        await q.edit_message_text(f'❌ {detail["error"]}')
        return

    await _render_detail(update, q, detail, st)


async def _render_detail(update, q, detail, st):
    """渲染详情卡片（带封面图 + 简介）"""
    domain = st.get('domain', 'ryu')

    if domain == 'ryu':
        lines = [f"🎮 <b>{h.escape(detail.get('title') or '')}</b>"]
        if detail.get('info_title'):
            lines.append(f"📖 {h.escape(detail['info_title'])}")
        if detail.get('developer'):
            lines.append(f"👨‍💻 {h.escape(detail['developer'])}")
        if detail.get('desc'):
            desc_t = await asyncio.to_thread(translate.translate_to_chinese, detail['desc'])
            lines.append(f"\n💬 {h.escape(desc_t[:300])}")
        btns = []
        price_units = int(os.environ.get('GAME_PRICE_UNITS', '100000000'))
        offer = _select_paid_offer(detail, price_units, _wallet_store)
        if offer:
            st.setdefault('paid_offers', {})[offer['resource_id']] = offer
            btns.append([_paid_download_button(offer)])
        for b in detail.get('download_buttons', [])[:8]:
            btns.append([InlineKeyboardButton(f'⬇️ {b["label"]}', url=b['url'])])
        if not btns:
            lines.append('\n⚠️ 暂无直接下载按钮')
            btns.append([InlineKeyboardButton('🔗 打开原站', url=detail.get('url', ''))])
        btns.append([InlineKeyboardButton('↩️ 返回列表', callback_data='page_refresh')])
        text = '\n'.join(lines)
        kb = InlineKeyboardMarkup(btns)

        # 带封面图发送
        thumb = detail.get('thumb') or ''
        img_bytes = None
        if thumb and thumb.startswith('http'):
            img_bytes = await asyncio.to_thread(_download_image, thumb)
        try:
            if img_bytes:
                await q.message.reply_photo(io.BytesIO(img_bytes), caption=text, parse_mode='HTML', reply_markup=kb)
            else:
                await q.message.reply_text(text, parse_mode='HTML', reply_markup=kb)
            await q.message.delete()
        except Exception:
            try:
                await q.edit_message_text(text, parse_mode='HTML', reply_markup=kb)
            except Exception:
                pass
    else:
        # BT domain：有番号封面时发送图片详情卡，匹配不到则保留文字卡。
        text, kb = _build_bt_detail(detail)
        cover = detail.get('cover') or ''
        img_bytes = None
        if cover.startswith('http'):
            img_bytes = await asyncio.to_thread(_download_image, cover)
        try:
            if img_bytes:
                await q.message.reply_photo(
                    io.BytesIO(img_bytes), caption=text, parse_mode='HTML', reply_markup=kb
                )
                await q.message.delete()
            else:
                await q.edit_message_text(text, parse_mode='HTML', reply_markup=kb)
        except Exception as e:
            logger.exception('BT 详情渲染失败')
            try:
                await q.edit_message_text(text, parse_mode='HTML', reply_markup=kb)
            except Exception:
                await q.message.reply_text(f'❌ 详情显示失败: {h.escape(str(e))}')


async def page_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    st = _state(q.from_user.id)
    await _render_page(update, q.message, st)


async def noop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        '🎴 <b>使用说明</b>\n\n'
        '1. 发送 /start 选择搜索域\n'
        '2. 输入关键词搜索，结果列表点选\n'
        '3. 点结果查看详情（带封面图+简介）\n'
        '4. 黄油：下载按钮直达镜像；BT：磁力一键复制\n'
        '5. /topup 金额：创建 USDT 充值订单\n\n'
        '💡 提示：BT 搜索直接输入番号 (如 MIDV-726) 更快~',
        parse_mode='HTML',
    )


def main():
    app = (Application.builder().token(TOKEN)
           .post_init(_post_init).post_shutdown(_post_shutdown).build())

    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('help', help_cmd))
    app.add_handler(CommandHandler('wallet', wallet_cmd))
    app.add_handler(CommandHandler('balance', wallet_cmd))
    app.add_handler(CommandHandler('topup', topup_cmd))
    app.add_handler(CommandHandler('recharge', topup_cmd))
    app.add_handler(CommandHandler('deposit', topup_cmd))
    app.add_handler(CallbackQueryHandler(wallet_cmd, pattern='^wallet_home$'))
    app.add_handler(CallbackQueryHandler(wallet_topup, pattern='^wallet_topup$'))
    app.add_handler(CallbackQueryHandler(check_topup, pattern='^checkpay_'))
    app.add_handler(CallbackQueryHandler(buy_download, pattern='^buy_'))
    app.add_handler(CallbackQueryHandler(domain_select, pattern='^domain_'))
    app.add_handler(CallbackQueryHandler(back_start, pattern='^back_start$'))
    app.add_handler(CallbackQueryHandler(page_nav, pattern='^page_'))
    app.add_handler(CallbackQueryHandler(pick_item, pattern='^pick_'))
    app.add_handler(CallbackQueryHandler(page_refresh, pattern='^page_refresh$'))
    app.add_handler(CallbackQueryHandler(noop, pattern='^noop$'))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, do_search))

    logger.info('Starting search bot...')
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
