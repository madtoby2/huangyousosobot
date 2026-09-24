import tempfile
import unittest
from pathlib import Path

from game_title_index import GameTitleIndex, extract_rj_codes, normalize_title


class GameTitleIndexTests(unittest.TestCase):
    def test_title_normalization_ignores_punctuation_and_width(self):
        self.assertEqual(normalize_title(' Ｇａｍｅ：ＡＢＣ！ '), 'gameabc')
        self.assertEqual(normalize_title('游戏！ 名称'), '游戏名称')

    def test_rj_codes_are_extracted_from_titles_and_image_urls(self):
        self.assertEqual(
            extract_rj_codes('https://img.example/RJ01234567_img_main.jpg RJ7654321'),
            ['RJ01234567', 'RJ7654321'],
        )

    def test_related_language_editions_expand_to_source_english_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = GameTitleIndex(Path(tmp) / 'games.sqlite3')
            records = {
                'RJ00010001': {
                    'work_name': 'Original title',
                    'translation_info': {
                        'original_workno': None,
                        'parent_workno': None,
                        'child_worknos': ['RJ00010002', 'RJ00010003'],
                        'lang': None,
                    },
                },
                'RJ00010002': {
                    'work_name': '中文游戏名',
                    'translation_info': {
                        'original_workno': 'RJ00010001',
                        'parent_workno': None,
                        'child_worknos': [],
                        'lang': 'CHI_HANS',
                    },
                },
                'RJ00010003': {
                    'work_name': 'English Edition Title',
                    'translation_info': {
                        'original_workno': 'RJ00010001',
                        'parent_workno': None,
                        'child_worknos': [],
                        'lang': 'ENG',
                    },
                },
            }
            index.add_product_records(records, source_aliases={
                'RJ00010001': ['English source title'],
            })

            terms = index.search_terms('中文游戏名')

            self.assertEqual(terms[0], 'English source title')
            self.assertIn('English Edition Title', terms)
            self.assertEqual(terms[-1], '中文游戏名')

    def test_unmatched_title_falls_back_to_original_search_term(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = GameTitleIndex(Path(tmp) / 'games.sqlite3')
            self.assertEqual(index.search_terms('未知中文名'), ['未知中文名'])

    def test_alias_index_survives_reopening(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'games.sqlite3'
            first = GameTitleIndex(path)
            first.add_product_records({
                'RJ00020001': {
                    'work_name': 'English title',
                    'translation_info': {'lang': 'ENG'},
                },
            })
            reopened = GameTitleIndex(path)
            self.assertIn('English title', reopened.search_terms('English title'))


class DLsiteFailureCacheTests(unittest.TestCase):
    def test_transient_catalog_error_is_retried_not_cached_as_no_match(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            index = GameTitleIndex(Path(td) / 'index.sqlite3')
            with patch.object(index, '_fetch_search_html', side_effect=RuntimeError('temporary failure')) as fetch:
                self.assertEqual(index.resolve_search_terms('暂时查不到的中文名'), ['暂时查不到的中文名'])
                self.assertEqual(index.resolve_search_terms('暂时查不到的中文名'), ['暂时查不到的中文名'])
            self.assertEqual(fetch.call_count, 2)


class StartHelpTests(unittest.TestCase):
    def test_start_explains_bilingual_search_and_english_title_fallback(self):
        from bot import _start_text
        text = _start_text()
        self.assertIn('中英名互搜', text)
        self.assertIn('原关键词搜索', text)
        self.assertIn('站点原标题', text)


class GameSearchIntegrationTests(unittest.TestCase):
    def test_combined_game_search_uses_english_alias_then_keeps_original_fallback(self):
        from unittest.mock import Mock, patch
        import bot

        index = Mock()
        index.resolve_search_terms.return_value = ['English source title', '中文游戏名']
        def source_search(query, limit):
            if query == 'English source title':
                return {'results': [{'title': 'English source title', 'url': 'https://site/game/1'}]}
            return {'results': [{'title': '中文游戏名', 'url': 'https://site/game/2'}]}

        with patch.object(bot.game_title_index, 'get_index', return_value=index), \
             patch.object(bot.search_ryuugames, 'search', side_effect=source_search) as ryu, \
             patch.object(bot.search_otomi, 'search', return_value={'results': []}):
            result = bot.combined_game_search('中文游戏名')

        self.assertEqual(index.resolve_search_terms.call_args.args, ('中文游戏名',))
        self.assertEqual([x['url'] for x in result['results']],
                         ['https://site/game/1', 'https://site/game/2'])
        self.assertIn(('English source title', 10), [call.args for call in ryu.call_args_list])
        self.assertIn(('中文游戏名', 10), [call.args for call in ryu.call_args_list])

    def test_combined_game_search_falls_back_to_typed_term_when_unmapped(self):
        from unittest.mock import Mock, patch
        import bot

        index = Mock()
        index.resolve_search_terms.return_value = ['未收录中文名']
        with patch.object(bot.game_title_index, 'get_index', return_value=index), \
             patch.object(bot.search_ryuugames, 'search', return_value={'results': []}) as ryu, \
             patch.object(bot.search_otomi, 'search', return_value={'results': []}):
            result = bot.combined_game_search('未收录中文名')

        self.assertEqual(result, {'results': []})
        self.assertEqual(ryu.call_args.args, ('未收录中文名', 10))


if __name__ == '__main__':
    unittest.main()
