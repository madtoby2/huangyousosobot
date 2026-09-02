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
from pathlib import Path

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

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
logger = logging.getLogger('searchbot')

_user_state = {}


def _state(user_id):
    return _user_state.setdefault(user_id, {})


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _state(update.effective_user.id).clear()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton('🎮 黄油搜索', callback_data='domain_ryu')],
        [InlineKeyboardButton('🔍 BT搜索', callback_data='domain_bt')],
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
    ])
    await q.edit_message_text(
        '👋 选择搜索类型：\n\n'
        '🎮 <b>黄油搜索</b> - 搜成人游戏 (Ryuugames/Otomi)\n'
        '🔍 <b>BT搜索</b> - 搜 BT 磁力资源 (Sukebei/JavDB)',
        parse_mode='HTML',
        reply_markup=kb,
    )


async def do_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """文本消息 -> 搜索"""
    user_id = update.effective_user.id
    keyword = (update.message.text or '').strip()
    if not keyword or keyword.startswith('/'):
        return

    st = _state(user_id)
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
    try:
        await status_msg.edit_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(btns))
    except Exception:
        await status_msg.edit_text(text[:300], parse_mode='HTML', reply_markup=InlineKeyboardMarkup(btns))


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
        '4. 黄油：下载按钮直达镜像；BT：磁力一键复制\n\n'
        '💡 提示：BT 搜索直接输入番号 (如 MIDV-726) 更快~',
        parse_mode='HTML',
    )


def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('help', help_cmd))
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
