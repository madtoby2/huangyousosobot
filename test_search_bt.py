#!/usr/bin/env python3
import unittest
from unittest.mock import patch

import search_bt


JAVDB_HTML = '''
<div class="item">
  <a href="/v/qDOVY6" class="box" title="作品标题">
    <div class="cover"><img loading="lazy" src="https://c0.jdbstatic.com/covers/qd/qDOVY6.jpg" /></div>
    <div class="video-title"><strong>NHDTB-706</strong> 作品标题</div>
    <div class="meta">09/08/2022</div>
  </a>
</div>
'''


class BtMetadataTests(unittest.TestCase):
    def test_extracts_code_from_noisy_torrent_title(self):
        title = '[Reducing Mosaic] NHDTB-706 男湯で出会った痴女っこ7'
        self.assertEqual(search_bt.extract_video_code(title), 'NHDTB-706')

    def test_parses_current_javdb_card_markup(self):
        results = search_bt._parse_javdb_search(JAVDB_HTML, 5)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['code'], 'NHDTB-706')
        self.assertEqual(results[0]['cover'], 'https://c0.jdbstatic.com/covers/qd/qDOVY6.jpg')
        self.assertEqual(results[0]['url'], 'https://javdb.com/v/qDOVY6')

    def test_enriches_bt_result_with_matching_cover(self):
        item = {
            'title': '[Reducing Mosaic] NHDTB-706 sample',
            'magnet': 'magnet:?xt=urn:btih:abc',
            'source': 'sukebei',
        }
        metadata = [{
            'title': 'NHDTB-706 作品标题',
            'code': 'NHDTB-706',
            'cover': 'https://c0.jdbstatic.com/covers/qd/qDOVY6.jpg',
            'release_date': '2022-09-08',
            'url': 'https://javdb.com/v/qDOVY6',
        }]
        with patch.object(search_bt, 'search_javdb', return_value={'results': metadata}):
            enriched = search_bt.enrich_bt_result(item)

        self.assertEqual(enriched['code'], 'NHDTB-706')
        self.assertEqual(enriched['cover'], metadata[0]['cover'])
        self.assertEqual(enriched['release_date'], '2022-09-08')
        self.assertEqual(enriched['magnet'], item['magnet'])


if __name__ == '__main__':
    unittest.main()
