#!/usr/bin/env python3
import unittest
from unittest.mock import Mock, patch

from bot import (_download_price_units, _paid_download_button,
                 _select_paid_offer)


class PaidUiTests(unittest.TestCase):
    def test_default_game_and_bt_download_price_is_point_one_usdt(self):
        with patch.dict('os.environ', {}, clear=True):
            self.assertEqual(_download_price_units('game'), 10_000_000)
            self.assertEqual(_download_price_units('bt'), 10_000_000)

    def detail(self, buttons):
        return {'title':'Test Game','source':'ryuugames','url':'https://ryuugames.com/test/',
                'version':'1.0','download_buttons':buttons}

    def test_mediafire_is_preferred_over_pixeldrain_and_mega(self):
        offer=_select_paid_offer(self.detail([
            {'label':'MegaNZ','url':'https://mega.nz/file/a#b'},
            {'label':'PixelDrain','url':'https://pixeldrain.com/u/ABC123'},
            {'label':'Mediafire','url':'https://www.mediafire.com/file/x/game/file'},
        ]),100000000)
        self.assertEqual(offer['download_url'],'https://www.mediafire.com/file/x/game/file')
        self.assertEqual(offer['price_units'],100000000)

    def test_pixeldrain_is_used_when_mediafire_absent(self):
        offer=_select_paid_offer(self.detail([
            {'label':'PixelDrain','url':'https://pixeldrain.com/u/ABC123'},
        ]),125000000)
        self.assertIn('pixeldrain.com',offer['download_url'])
        button=_paid_download_button(offer)
        self.assertIn('1.25 USDT',button.text)
        self.assertLessEqual(len(button.callback_data.encode()),64)

    def test_existing_resource_uses_admin_price(self):
        store=Mock()
        store.get_resource.return_value={'price_units':125000000}
        offer=_select_paid_offer(self.detail([
            {'label':'Mediafire','url':'https://www.mediafire.com/file/x/game/file'},
        ]),100000000,store)
        self.assertEqual(offer['price_units'],125000000)

    def test_unsupported_only_has_no_paid_offer(self):
        offer=_select_paid_offer(self.detail([
            {'label':'MegaNZ','url':'https://mega.nz/file/a#b'},
            {'label':'Datanodes','url':'https://datanodes.to/abc'},
        ]),100000000)
        self.assertIsNone(offer)


if __name__=='__main__': unittest.main()
