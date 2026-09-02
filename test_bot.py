#!/usr/bin/env python3
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

from bot import _build_bt_detail, _render_detail


class BtDetailTests(unittest.TestCase):
    def test_magnet_uses_copy_text_button_not_url(self):
        magnet = 'magnet:?xt=urn:btih:47a51b8012cd969076ae0a3ae7c654'
        text, keyboard = _build_bt_detail({
            'title': '[Reducing Mosaic] NHDTB-706',
            'seeders': '39',
            'source_label': '🌰 Sukebei',
            'url': 'https://sukebei.nyaa.si/view/123',
            'magnet': magnet,
        })
        buttons = [button for row in keyboard.inline_keyboard for button in row]
        copy_buttons = [button for button in buttons if button.copy_text]
        self.assertIn('NHDTB-706', text)
        self.assertEqual(len(copy_buttons), 1)
        self.assertEqual(copy_buttons[0].copy_text.text, magnet)
        self.assertFalse(any(button.url and button.url.startswith('magnet:') for button in buttons))

    def test_long_magnet_is_reduced_to_valid_btih_link(self):
        magnet = (
            'magnet:?xt=urn:btih:47a51b8012cd969076ae0a3ae7c654'
            '&dn=' + 'NHDTB-706-' * 30 +
            '&tr=http%3A%2F%2Ftracker.example%2Fannounce'
        )
        _, keyboard = _build_bt_detail({
            'title': '[Reducing Mosaic] NHDTB-706',
            'url': 'https://sukebei.nyaa.si/view/123',
            'magnet': magnet,
        })
        copied = keyboard.inline_keyboard[0][0].copy_text.text
        self.assertEqual(copied, 'magnet:?xt=urn:btih:47a51b8012cd969076ae0a3ae7c654')
        self.assertLessEqual(len(copied), 256)

    def test_metadata_is_shown_in_bt_detail(self):
        text, keyboard = _build_bt_detail({
            'title': '[Reducing Mosaic] NHDTB-706',
            'code': 'NHDTB-706',
            'release_date': '2022-09-08',
            'metadata_url': 'https://javdb.com/v/qDOVY6',
            'url': 'https://sukebei.nyaa.si/view/123',
            'magnet': 'magnet:?xt=urn:btih:abc',
        })
        urls = [b.url for row in keyboard.inline_keyboard for b in row if b.url]
        self.assertIn('番号: NHDTB-706', text)
        self.assertIn('发行: 2022-09-08', text)
        self.assertIn('https://javdb.com/v/qDOVY6', urls)


class BtPhotoDetailTests(unittest.IsolatedAsyncioTestCase):
    async def test_bt_cover_is_sent_as_photo(self):
        message = SimpleNamespace(reply_photo=AsyncMock(), delete=AsyncMock())
        q = SimpleNamespace(message=message, edit_message_text=AsyncMock())
        detail = {
            'title': '[Reducing Mosaic] NHDTB-706',
            'cover': 'https://c0.jdbstatic.com/covers/qd/qDOVY6.jpg',
            'url': 'https://sukebei.nyaa.si/view/123',
            'magnet': 'magnet:?xt=urn:btih:abc',
        }
        with patch('bot._download_image', return_value=b'image-bytes'):
            await _render_detail(None, q, detail, {'domain': 'bt'})
        message.reply_photo.assert_awaited_once()
        message.delete.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
