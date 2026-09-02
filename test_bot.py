#!/usr/bin/env python3
import unittest

from bot import _build_bt_detail


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


if __name__ == '__main__':
    unittest.main()
