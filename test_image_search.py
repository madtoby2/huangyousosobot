#!/usr/bin/env python3
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot


class ImageSearchContractTests(unittest.TestCase):
    def test_trusted_matches_filters_low_confidence_and_sorts(self):
        self.assertTrue(hasattr(bot, '_trusted_whos_matches'))
        matches = bot._trusted_whos_matches({
            'matches': [
                {'code': 'LOW-001', 'similarity': 89.9},
                {'code': 'GOOD-002', 'similarity': 94.0},
                {'code': 'BEST-003', 'similarity': 99.0},
                {'code': '', 'similarity': 100.0},
            ]
        }, minimum=90)
        self.assertEqual([item['code'] for item in matches], ['BEST-003', 'GOOD-002'])

    def test_yandex_code_prefers_explicit_hyphenated_catalog_number(self):
        result = {'sites': [
            {'title': 'Experienced performer films them 281 GANA-2823',
             'url': 'https://example.test/watch/GANA-2823'}
        ]}
        self.assertEqual(bot._code_from_yandex(result), 'GANA-2823')

    def test_start_copy_explains_photo_to_bt_flow(self):
        self.assertTrue(hasattr(bot, '_start_text'))
        text = bot._start_text()
        self.assertIn('直接发送截图', text)
        self.assertIn('BT', text)


class ImageSearchHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_photo_match_routes_recognized_code_into_bt_results(self):
        self.assertTrue(hasattr(bot, 'handle_photo'))
        status = SimpleNamespace(edit_text=AsyncMock(), text='searching')
        message = SimpleNamespace(
            photo=[SimpleNamespace(file_id='photo-large')],
            document=None,
            reply_text=AsyncMock(return_value=status),
        )
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=4242),
            message=message,
        )
        tg_file = SimpleNamespace(download_to_drive=AsyncMock())
        context = SimpleNamespace(bot=SimpleNamespace(get_file=AsyncMock(return_value=tg_file)))
        bt_results = {'results': [{
            'title': 'GANA-2823 sample', 'magnet': 'magnet:?xt=urn:btih:abc',
            'url': 'https://sukebei.nyaa.si/view/1', 'source': 'sukebei',
            'source_label': 'Sukebei',
        }]}
        whos_result = {'matches': [{'code': 'GANA-2823', 'similarity': 99.1}]}

        with patch.object(bot, 'WHOS_TV_USERNAME', 'user'), \
             patch.object(bot, 'WHOS_TV_PASSWORD', 'secret'), \
             patch.object(bot, '_run_whos_search', return_value=whos_result), \
             patch.object(bot, '_run_yandex_search', return_value=None), \
             patch.object(bot.search_bt, 'search', return_value=bt_results), \
             patch.object(bot, '_render_page', new=AsyncMock()) as render:
            await bot.handle_photo(update, context)

        self.assertEqual(bot._state(4242)['domain'], 'bt')
        self.assertEqual(bot._state(4242)['keyword'], 'GANA-2823')
        self.assertEqual(bot._state(4242)['results'], bt_results['results'])
        render.assert_awaited_once()

    async def test_low_confidence_match_is_not_used_for_bt_search(self):
        self.assertTrue(hasattr(bot, 'handle_photo'))
        status = SimpleNamespace(edit_text=AsyncMock(), text='searching')
        message = SimpleNamespace(
            photo=[SimpleNamespace(file_id='photo-large')],
            document=None,
            reply_text=AsyncMock(return_value=status),
        )
        update = SimpleNamespace(effective_user=SimpleNamespace(id=4343), message=message)
        tg_file = SimpleNamespace(download_to_drive=AsyncMock())
        context = SimpleNamespace(bot=SimpleNamespace(get_file=AsyncMock(return_value=tg_file)))

        with patch.object(bot, 'WHOS_TV_USERNAME', 'user'), \
             patch.object(bot, 'WHOS_TV_PASSWORD', 'secret'), \
             patch.object(bot, '_run_whos_search', return_value={
                 'matches': [{'code': 'WRONG-001', 'similarity': 62.0}]
             }), patch.object(bot, '_run_yandex_search', return_value=None), \
             patch.object(bot.search_bt, 'search') as bt_search:
            await bot.handle_photo(update, context)

        bt_search.assert_not_called()
        final_text = status.edit_text.await_args.args[0]
        self.assertIn('没有找到可信匹配', final_text)

    async def test_yandex_code_falls_back_to_bt_when_whos_has_no_trusted_match(self):
        self.assertTrue(hasattr(bot, '_run_yandex_search'))
        status = SimpleNamespace(edit_text=AsyncMock(), text='searching')
        message = SimpleNamespace(photo=[SimpleNamespace(file_id='photo-large')], document=None,
                                  reply_text=AsyncMock(return_value=status))
        update = SimpleNamespace(effective_user=SimpleNamespace(id=4444), message=message)
        tg_file = SimpleNamespace(download_to_drive=AsyncMock())
        context = SimpleNamespace(bot=SimpleNamespace(get_file=AsyncMock(return_value=tg_file)))
        yandex_result = {'sites': [{'title': 'GANA-2823 sample scene',
                                    'url': 'https://example.test/GANA-2823'}]}
        bt_results = {'results': [{'title': 'GANA-2823 torrent', 'magnet': 'magnet:?xt=urn:btih:abc',
                                   'url': 'https://sukebei.nyaa.si/view/1', 'source': 'sukebei'}]}
        with patch.object(bot, 'WHOS_TV_USERNAME', 'user'), \
             patch.object(bot, 'WHOS_TV_PASSWORD', 'secret'), \
             patch.object(bot, '_run_whos_search', return_value=None), \
             patch.object(bot, '_run_yandex_search', return_value=yandex_result), \
             patch.object(bot.search_bt, 'search', return_value=bt_results), \
             patch.object(bot, '_render_page', new=AsyncMock()) as render:
            await bot.handle_photo(update, context)
        self.assertEqual(bot._state(4444)['keyword'], 'GANA-2823')
        render.assert_awaited_once()

    async def test_image_document_preserves_supported_suffix(self):
        status = SimpleNamespace(edit_text=AsyncMock(), text='searching')
        message = SimpleNamespace(
            photo=[], document=SimpleNamespace(file_id='doc-image', file_name='frame.png', mime_type='image/png'),
            reply_text=AsyncMock(return_value=status),
        )
        update = SimpleNamespace(effective_user=SimpleNamespace(id=4545), message=message)
        tg_file = SimpleNamespace(download_to_drive=AsyncMock())
        context = SimpleNamespace(bot=SimpleNamespace(get_file=AsyncMock(return_value=tg_file)))
        with patch.object(bot, '_run_whos_search', return_value=None), \
             patch.object(bot, '_run_yandex_search', return_value=None):
            await bot.handle_photo(update, context)
        path = tg_file.download_to_drive.await_args.kwargs['custom_path']
        self.assertTrue(path.endswith('.png'), path)


if __name__ == '__main__':
    unittest.main()
