#!/usr/bin/env python3
import tempfile
import unittest
from unittest import mock

try:
    import yandex_images
except ImportError:
    yandex_images = None


SAMPLE = {"blocks": [{"params": {"originalImageUrl": "https://img.test/orig", "cbirId": "1/abc"}}]}
SAMPLE_HTML = '''
<div class="CbirSites-Item">
 <a class="Link Thumb" href="https://img.test/a.jpg">image</a>
 <a class="Link Link_view_default" href="https://example.com/GANA-2823?utm_source=yandex">GANA-2823 sample</a>
 <a class="Link CbirSites-ItemDomain" href="https://example.com/">example.com</a>
</div>
'''


class YandexImageTests(unittest.TestCase):
    def test_module_exists(self):
        self.assertIsNotNone(yandex_images)

    def test_upload_and_site_parsing(self):
        self.assertIsNotNone(yandex_images)
        parsed = yandex_images.parse_upload(SAMPLE)
        self.assertIn('cbir_id=1%2Fabc', parsed['search_url'])
        sites = yandex_images.parse_sites(SAMPLE_HTML)
        self.assertEqual(sites[0]['title'], 'GANA-2823 sample')
        self.assertEqual(sites[0]['url'], 'https://example.com/GANA-2823')

    def test_search_posts_image_and_fetches_result_cards(self):
        self.assertIsNotNone(yandex_images)
        upload=mock.Mock(); upload.json.return_value=SAMPLE; upload.raise_for_status=mock.Mock()
        page=mock.Mock(text=SAMPLE_HTML); page.raise_for_status=mock.Mock()
        with tempfile.NamedTemporaryFile(suffix='.jpg') as image, \
             mock.patch('requests.post', return_value=upload), \
             mock.patch('requests.get', return_value=page):
            result=yandex_images.search(image.name)
        self.assertEqual(result['sites'][0]['title'], 'GANA-2823 sample')


if __name__ == '__main__': unittest.main()
