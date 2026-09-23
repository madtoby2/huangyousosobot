#!/usr/bin/env python3
import unittest
from unittest import mock

try:
    import whos_daily_tasks
except ImportError:
    whos_daily_tasks = None

VIDEO_LIST = '<a href="/videos/code-a">A</a><a href="/videos/code-b">B</a>'
VIDEO_A = """<button onclick="showRating('101', 'A', '0', '0', '0')"></button><button data-id="101" data-favorite="false"></button>"""
VIDEO_B = """<button onclick="showRating('102', 'B', '0', '0', '0')"></button><button data-id="102" data-favorite="false"></button>"""
DONE = """<button onclick="showRating('201', 'A', '0', '0', '5')"></button><button data-id="201" data-favorite="true"></button>"""
FRESH = """<button onclick="showRating('202', 'B', '0', '0', '0')"></button><button data-id="202" data-favorite="false"></button>"""
FRAME_ROOT = '<a href="/frames/type-clothes/label-632653">A</a><a href="/frames/type-clothes/label-632433">B</a>'
FRAME_PAGE = '<button class="frame-rate-btn" data-frame-id="501" data-my-rating="0"></button>'
FRAME_PAGE_2 = '<button class="frame-rate-btn" data-frame-id="502" data-my-rating="0"></button>'


def response(payload=None, text=''):
    obj = mock.Mock(text=text)
    obj.raise_for_status = mock.Mock()
    obj.json.return_value = payload or {'code': 200000, 'data': {}}
    return obj


class WhosDailyTaskTests(unittest.TestCase):
    def test_module_exists(self):
        self.assertIsNotNone(whos_daily_tasks)

    def test_parse_video_candidate_reads_id_rating_and_favorite(self):
        item = whos_daily_tasks.parse_video_candidate(VIDEO_A, 'code-a')
        self.assertEqual(item, {'slug': 'code-a', 'id': '101', 'my_rating': 0, 'is_favorite': False})

    def test_frame_candidates_extracts_unrated_frame(self):
        session = mock.Mock()
        session.get.side_effect = [response(text=FRAME_ROOT), response(text=FRAME_PAGE)]
        items = whos_daily_tasks._frame_candidates(session, 1)
        self.assertEqual(items, [{'id': '501', 'my_rating': 0, 'path': '/frames/type-clothes/label-632653'}])

    def test_rating_submission_uses_form_encoding(self):
        session = mock.Mock()
        session.post.return_value = response({'code': 200000})
        whos_daily_tasks._post_ok(session, '/api/frames/501/rate', data={'score': 4})
        self.assertEqual(session.post.call_args.kwargs.get('data'), {'score': 4})
        self.assertNotIn('json', session.post.call_args.kwargs)

    def test_candidate_fetch_scans_past_ineligible_videos(self):
        listed = '<a href="/videos/old">old</a><a href="/videos/new">new</a>'
        session = mock.Mock()
        session.get.side_effect = [response(text=listed), response(text=DONE), response(text=FRESH)]
        items = whos_daily_tasks._video_candidates(session, 0, 1)
        self.assertEqual([item['id'] for item in items], ['202'])
        self.assertEqual(session.get.call_count, 3)

    def test_candidate_fetch_uses_next_listing_page_when_first_page_is_exhausted(self):
        first_list = '''<a href="/videos/old">old</a><a
 href="/videos/page-2"
 data-active="false" class="btn"><span>2</span></a>'''
        next_list = '<a href="/videos/new">new</a>'
        session = mock.Mock()
        session.get.side_effect = [response(text=first_list), response(text=DONE),
                                   response(text=next_list), response(text=FRESH)]
        items = whos_daily_tasks._video_candidates(session, 0, 1)
        self.assertEqual([item['id'] for item in items], ['202'])
        self.assertEqual(session.get.call_args_list[2].args[0], 'https://whos.tv/videos/page-2')

    def test_failure_summary_keeps_actionable_message_but_redacts_credentials(self):
        exc = RuntimeError('login failed for user123 with pass123; HTTP 401')
        summary = whos_daily_tasks._failure_summary(exc, {'username': 'user123', 'password': 'pass123'})
        self.assertIn('HTTP 401', summary)
        self.assertNotIn('user123', summary)
        self.assertNotIn('pass123', summary)

    def test_failure_summary_is_bounded(self):
        summary = whos_daily_tasks._failure_summary(RuntimeError('x' * 1000), {})
        self.assertLessEqual(len(summary), 240)

    def test_complete_daily_tasks_rates_frames_and_verifies_completion(self):
        session = mock.Mock()
        before = {'data': {'daily': [
            {'task_key': 'daily_signin', 'max_completions': 1, 'progress': {'today_completions': 0}},
            {'task_key': 'task_frame_rating', 'max_completions': 2, 'progress': {'today_completions': 0}},
            {'task_key': 'task_favorite_content', 'max_completions': 2, 'progress': {'today_completions': 0}},
            {'task_key': 'task_share', 'max_completions': 2, 'progress': {'today_completions': 0}},
        ]}}
        after = {'data': {'daily': [
            {'task_key': 'daily_signin', 'max_completions': 1, 'progress': {'today_completions': 1, 'status': 2}},
            {'task_key': 'task_frame_rating', 'max_completions': 2, 'progress': {'today_completions': 2, 'status': 2}},
            {'task_key': 'task_favorite_content', 'max_completions': 2, 'progress': {'today_completions': 2, 'status': 2}},
            {'task_key': 'task_share', 'max_completions': 2, 'progress': {'today_completions': 2, 'status': 2}},
        ]}}
        profile = {'data': {'points_balance': 180}}
        session.get.side_effect = [response(before), response(text=FRAME_ROOT), response(text=FRAME_PAGE),
            response(text=FRAME_PAGE_2), response(text=VIDEO_LIST), response(text=VIDEO_A),
            response(text=VIDEO_B), response(after), response(profile)]
        session.post.return_value = response({'code': 200000})
        result = whos_daily_tasks.complete_daily_tasks(session, 'user123', 'pass123')
        paths = [call.args[0] for call in session.post.call_args_list]
        self.assertIn('https://whos.tv/api/user/tasks/signin', paths)
        self.assertIn('https://whos.tv/api/frames/501/rate', paths)
        self.assertIn('https://whos.tv/api/frames/502/rate', paths)
        self.assertIn('https://whos.tv/api/videos/101/favorite', paths)
        self.assertEqual(paths.count('https://whos.tv/api/user/tasks/task_share/complete'), 2)
        self.assertEqual(result['points_balance'], 180)
        self.assertTrue(result['all_complete'])

    def test_already_complete_tasks_do_not_generate_actions(self):
        session = mock.Mock()
        done = {'data': {'daily': [
            {'task_key': key, 'max_completions': maxn,
             'progress': {'today_completions': maxn, 'status': 2}}
            for key, maxn in [('daily_signin', 1), ('task_frame_rating', 5),
                              ('task_favorite_content', 5), ('task_share', 5)]
        ]}}
        session.get.side_effect = [response(done), response(done), response({'data': {'points_balance': 99}})]
        session.post.return_value = response({'code': 200000})
        result = whos_daily_tasks.complete_daily_tasks(session, 'user123', 'pass123')
        self.assertEqual(session.post.call_count, 1)
        self.assertTrue(result['all_complete'])


if __name__ == '__main__':
    unittest.main()
