import asyncio
import os
import tempfile
from pathlib import Path
import re
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import bot
import search_bt

UTC8 = timezone(timedelta(hours=8))
SUKBEBEI_HTML = '''
<table><tbody>
<tr class="default">
  <td><a href="/?c=2_2">x</a></td>
  <td><a href="/view/4720671">[Test] Sample title</a></td>
  <td class="text-center"><a href="magnet:?xt=urn:btih:abc123&dn=sample"></a></td>
  <td class="text-center">3.1 GiB</td>
  <td class="text-center" data-timestamp="1790236091">2026-09-24 07:48</td>
  <td class="text-center">1,234</td>
  <td class="text-center">4</td>
  <td class="text-center">0</td>
</tr>
</tbody></table>
'''


def require_callable(test, module, name):
    function = getattr(module, name, None)
    test.assertTrue(callable(function), f'{module.__name__}.{name} must be implemented')
    return function


def torrent(torrent_id, uploaded_ts, seeders):
    return {
        'torrent_id': str(torrent_id),
        'title': f'Torrent {torrent_id}',
        'url': f'https://sukebei.nyaa.si/view/{torrent_id}',
        'magnet': f'magnet:?xt=urn:btih:{torrent_id}',
        'seeders': str(seeders),
        'seeders_count': seeders,
        'uploaded_ts': uploaded_ts,
        'source': 'sukebei',
        'source_label': '🌰 Sukebei',
    }


class SukebeiDiscoveryTests(unittest.TestCase):
    def test_parser_extracts_timestamp_seeder_count_and_torrent_id(self):
        parser = require_callable(self, search_bt, 'parse_sukebei_results')
        result = parser(SUKBEBEI_HTML)[0]
        self.assertEqual(result['torrent_id'], '4720671')
        self.assertEqual(result['uploaded_ts'], 1790236091)
        self.assertEqual(result['seeders_count'], 1234)
        self.assertEqual(result['magnet'], 'magnet:?xt=urn:btih:abc123&dn=sample')
        self.assertEqual(result['title'], '[Test] Sample title')

    def test_daily_ranking_scans_until_before_utc8_day_and_sorts_by_seeders(self):
        require_callable(self, search_bt, 'daily_ranking')
        now = datetime(2026, 9, 24, 12, 0, tzinfo=UTC8)
        midnight = int(datetime(2026, 9, 24, 0, 0, tzinfo=UTC8).timestamp())
        lower = torrent(10, midnight + 100, 4)
        higher = torrent(11, midnight + 200, 31)
        yesterday = torrent(9, midnight - 1, 999)
        with patch.object(search_bt, '_fetch_sukebei_page', side_effect=[
            [lower, higher], [yesterday],
        ]) as fetch:
            report = search_bt.daily_ranking(limit=10, max_pages=4, now=now)
        self.assertEqual([x['torrent_id'] for x in report['results']], ['11', '10'])
        self.assertEqual(report['total_today'], 2)
        self.assertEqual(report['date'], '2026-09-24')
        self.assertTrue(report['complete'])
        self.assertEqual(fetch.call_count, 2)
        self.assertTrue(all(call.kwargs['sort'] == 'id' for call in fetch.call_args_list))

    def test_daily_ranking_marks_page_cap_as_incomplete(self):
        require_callable(self, search_bt, 'daily_ranking')
        now = datetime(2026, 9, 24, 12, 0, tzinfo=UTC8)
        midnight = int(datetime(2026, 9, 24, 0, 0, tzinfo=UTC8).timestamp())
        with patch.object(search_bt, '_fetch_sukebei_page', return_value=[torrent(1, midnight + 10, 2)]):
            report = search_bt.daily_ranking(limit=10, max_pages=1, now=now)
        self.assertFalse(report['complete'])
        self.assertEqual(report['scanned_pages'], 1)

    def test_random_recommendation_chooses_from_recent_uploads_only(self):
        require_callable(self, search_bt, 'random_recommendation')
        now = datetime(2026, 9, 24, 12, 0, tzinfo=UTC8)
        now_ts = int(now.timestamp())
        first = torrent(1, now_ts - 3600, 2)
        second = torrent(2, now_ts - 6 * 86400, 8)
        old = torrent(3, now_ts - 8 * 86400, 100)
        with patch.object(search_bt, '_fetch_sukebei_page', side_effect=[[first, second], [old]]):
            with patch.object(search_bt.random, 'choice', return_value=second):
                report = search_bt.random_recommendation(lookback_days=7, max_pages=4, now=now)
        self.assertEqual(report['result']['torrent_id'], '2')
        self.assertEqual(report['candidate_count'], 2)
        self.assertTrue(report['complete'])


class BtDiscoveryUiTests(unittest.IsolatedAsyncioTestCase):
    async def test_bt_domain_menu_shows_daily_rank_and_random_recommendation(self):
        q = SimpleNamespace(
            answer=AsyncMock(), data='domain_bt',
            from_user=SimpleNamespace(id=701), edit_message_text=AsyncMock(),
        )
        with patch.object(bot, '_state', return_value={}):
            await bot.domain_select(SimpleNamespace(callback_query=q), None)
        markup = q.edit_message_text.await_args.kwargs['reply_markup']
        callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
        self.assertIn('bt_daily', callbacks)
        self.assertIn('bt_random', callbacks)

    async def test_daily_button_stores_results_and_marks_seeder_rank_scope(self):
        require_callable(self, bot, 'bt_discovery')
        rows = [torrent(1, 1790236091, 12)]
        state = {}
        q = SimpleNamespace(
            answer=AsyncMock(), data='bt_daily',
            from_user=SimpleNamespace(id=702),
            edit_message_text=AsyncMock(return_value=SimpleNamespace(text='loading')),
        )
        report = {'results': rows, 'date': '2026-09-24', 'complete': True,
                  'scanned_pages': 2, 'total_today': 1}
        with patch.object(bot, '_state', return_value=state), \
             patch.object(bot.search_bt, 'daily_ranking', return_value=report), \
             patch.object(bot, '_render_page', new_callable=AsyncMock) as render:
            await bot.bt_discovery(SimpleNamespace(callback_query=q), None)
        self.assertEqual(state['results'], rows)
        self.assertIn('今日', state['list_title'])
        self.assertIn('做种数', state['list_notice'])
        render.assert_awaited_once()

    async def test_random_button_stores_one_selected_result(self):
        require_callable(self, bot, 'bt_discovery')
        chosen = torrent(3, 1790236091, 6)
        state = {}
        q = SimpleNamespace(
            answer=AsyncMock(), data='bt_random',
            from_user=SimpleNamespace(id=703),
            edit_message_text=AsyncMock(return_value=SimpleNamespace(text='loading')),
        )
        report = {'result': chosen, 'candidate_count': 42, 'complete': True,
                  'scanned_pages': 3, 'lookback_days': 7}
        with patch.object(bot, '_state', return_value=state), \
             patch.object(bot.search_bt, 'random_recommendation', return_value=report), \
             patch.object(bot, '_render_page', new_callable=AsyncMock) as render:
            await bot.bt_discovery(SimpleNamespace(callback_query=q), None)
        self.assertEqual(state['results'], [chosen])
        self.assertIn('随机推荐', state['list_title'])
        self.assertEqual(state['page'], 0)
        render.assert_awaited_once()


class HandlerRegistrationTests(unittest.TestCase):
    def test_daily_and_random_callbacks_are_registered(self):
        app = MagicMock()
        builder = MagicMock()
        builder.token.return_value = builder
        builder.post_init.return_value = builder
        builder.post_shutdown.return_value = builder
        builder.build.return_value = app
        with patch.object(bot.Application, 'builder', return_value=builder):
            bot.main()

        handlers = [call.args[0] for call in app.add_handler.call_args_list]
        discovery_handlers = [
            handler for handler in handlers
            if isinstance(handler, bot.CallbackQueryHandler)
            and handler.callback is bot.bt_discovery
        ]
        self.assertEqual(len(discovery_handlers), 1)
        pattern = discovery_handlers[0].pattern
        regex = pattern if hasattr(pattern, 'search') else re.compile(pattern)
        self.assertIsNotNone(regex.search('bt_daily'))
        self.assertIsNotNone(regex.search('bt_random'))
        self.assertIsNone(regex.search('bt_unknown'))


class BtDailyPushTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _report():
        return {
            'date': '2026-09-24',
            'total_today': 1,
            'results': [{
                'torrent_id': '4720671',
                'title': 'A <B & C>',
                'url': 'https://sukebei.nyaa.si/view/4720671',
                'seeders_count': 8,
            }],
            'complete': False,
            'scanned_pages': 20,
        }

    async def test_push_time_rolls_at_utc8_twenty_one_hundred(self):
        next_push = require_callable(self, bot, '_next_bt_daily_push')
        before = datetime(2026, 9, 24, 20, 59, 59, tzinfo=UTC8)
        at_time = datetime(2026, 9, 24, 21, 0, tzinfo=UTC8)
        self.assertEqual(next_push(before), datetime(2026, 9, 24, 21, 0, tzinfo=UTC8))
        self.assertEqual(next_push(at_time), datetime(2026, 9, 25, 21, 0, tzinfo=UTC8))

    async def test_daily_message_escapes_titles_and_marks_partial_scan(self):
        format_message = require_callable(self, bot, '_format_daily_bt_message')
        text = format_message(self._report())
        self.assertIn('2026-09-24', text)
        self.assertIn('A &lt;B &amp; C&gt;', text)
        self.assertIn('https://sukebei.nyaa.si/view/4720671', text)
        self.assertIn('8', text)
        self.assertIn('⚠️', text)

    async def test_missing_channel_admin_permission_skips_without_fetch_or_send(self):
        publish = require_callable(self, bot, '_publish_daily_bt_ranking')
        api = SimpleNamespace(
            id=8900689851,
            get_chat_member=AsyncMock(return_value=SimpleNamespace(
                status='member', can_post_messages=False)),
            send_message=AsyncMock(),
        )
        with patch.object(bot.search_bt, 'daily_ranking', return_value=self._report()) as ranking:
            sent = await publish(SimpleNamespace(bot=api))
        self.assertFalse(sent)
        ranking.assert_not_called()
        api.send_message.assert_not_awaited()

    async def test_channel_admin_receives_formatted_daily_ranking(self):
        publish = require_callable(self, bot, '_publish_daily_bt_ranking')
        api = SimpleNamespace(
            id=8900689851,
            get_chat_member=AsyncMock(return_value=SimpleNamespace(
                status='administrator', can_post_messages=True)),
            send_message=AsyncMock(),
        )
        with patch.object(bot.search_bt, 'daily_ranking', return_value=self._report()):
            sent = await publish(SimpleNamespace(bot=api))
        self.assertTrue(sent)
        kwargs = api.send_message.await_args.kwargs
        self.assertEqual(str(kwargs['chat_id']), '-1003863698613')
        self.assertIn('今日新种', kwargs['text'])
        self.assertIn('A &lt;B &amp; C&gt;', kwargs['text'])
        self.assertEqual(kwargs['parse_mode'], 'HTML')

    async def test_daily_video_publisher_copies_one_video_after_permission_check(self):
        publish = require_callable(self, bot, '_publish_daily_bt_video')
        item={'torrent_id':'4720671','title':'Licensed Daily Video','magnet':'magnet:?xt=urn:btih:'+'a'*40,'source':'sukebei'}
        report={'date':'2026-09-24','results':[item]}
        api=SimpleNamespace(
            id=8900689851,
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status='administrator',can_post_messages=True)),
            copy_message=AsyncMock(return_value=SimpleNamespace(message_id=777,chat=SimpleNamespace(id=-1003863698613))),
        )
        user_uploader=SimpleNamespace(upload_video=AsyncMock(return_value={
            'storage_chat_id':-100777,'storage_message_id':666,
        }))
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,{'DOWNLOAD_DIR':tmp}), \
             patch.object(bot.search_bt,'daily_ranking',return_value=report) as ranking, \
             patch.object(bot,'build_telethon_uploader',return_value=user_uploader) as build, \
             patch.object(bot,'_download_daily_video') as download:
            def write_video(magnet,directory,title):
                path=Path(directory)/'one.mp4'; path.write_bytes(b'video')
                return {'path':str(path),'file_size':5}
            download.side_effect=write_video
            sent=await publish(SimpleNamespace(bot=api))
        self.assertTrue(sent)
        ranking.assert_called_once_with(limit=1)
        build.assert_called_once_with()
        download.assert_called_once()
        caption=user_uploader.upload_video.await_args.kwargs['caption']
        self.assertIn('Licensed Daily Video',caption)
        self.assertNotIn('magnet:',caption)
        api.copy_message.assert_awaited_once_with(
            chat_id='-1003863698613',from_chat_id=-100777,message_id=666)
        self.assertFalse(Path(tmp).exists())

    async def test_daily_video_publisher_fails_closed_before_download_if_permission_unverified(self):
        publish=require_callable(self,bot,'_publish_daily_bt_video')
        api=SimpleNamespace(id=8900689851,get_chat_member=AsyncMock(side_effect=RuntimeError('member list is inaccessible')),
                            copy_message=AsyncMock())
        with patch.object(bot.search_bt,'daily_ranking') as ranking, \
             patch.object(bot,'build_telethon_uploader') as build, \
             patch.object(bot,'_download_daily_video') as download:
            sent=await publish(SimpleNamespace(bot=api))
        self.assertFalse(sent)
        ranking.assert_not_called()
        build.assert_not_called()
        download.assert_not_called()
        api.copy_message.assert_not_awaited()

    async def test_daily_video_publisher_skips_non_sukebei_candidates(self):
        publish=require_callable(self,bot,'_publish_daily_bt_video')
        api=SimpleNamespace(id=8900689851,
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status='administrator',can_post_messages=True)),
            copy_message=AsyncMock())
        report={'date':'2026-09-24','results':[{'title':'other','magnet':'magnet:?xt=urn:btih:'+'b'*40,'source':'other'}]}
        with patch.object(bot.search_bt,'daily_ranking',return_value=report), \
             patch.object(bot,'build_telethon_uploader') as build, \
             patch.object(bot,'_download_daily_video') as download:
            sent=await publish(SimpleNamespace(bot=api))
        self.assertFalse(sent)
        build.assert_not_called()
        download.assert_not_called()
        api.copy_message.assert_not_awaited()

    async def test_application_does_not_start_unapproved_daily_push(self):
        application = SimpleNamespace(bot_data={})
        with patch.object(bot, 'BT_DAILY_PUSH_ENABLED', False, create=True), \
             patch.object(bot, 'OKPAY_SHOP_ID', ''), \
             patch.object(bot, 'OKPAY_API_KEY', ''), \
             patch.object(bot, '_delivery_configured', return_value=False):
            await bot._post_init(application)
        self.assertNotIn('bt_daily_push_task', application.bot_data)

    async def test_application_starts_and_cancels_daily_push_loop(self):
        application = SimpleNamespace(bot_data={})
        with patch.object(bot, 'BT_DAILY_PUSH_ENABLED', True, create=True), \
             patch.object(bot, 'OKPAY_SHOP_ID', ''), \
             patch.object(bot, 'OKPAY_API_KEY', ''), \
             patch.object(bot, '_delivery_configured', return_value=False):
            await bot._post_init(application)
        task = application.bot_data.get('bt_daily_push_task')
        self.assertIsNotNone(task)
        await bot._post_shutdown(application)
        with self.assertRaises(asyncio.CancelledError):
            await task


if __name__ == '__main__':
    unittest.main()
