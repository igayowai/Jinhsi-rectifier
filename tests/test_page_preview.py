from contextlib import ExitStack, redirect_stdout, redirect_stderr
from dataclasses import replace
from io import StringIO
import json
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from rectifier.__main__ import main
from rectifier.models import AuthorKind
from rectifier.parser import parse_board
from rectifier.policy import MAX_TARGETS, build_plan, preview_page_items
from rectifier.public_board import PublicReadError
from tests.test_parser import board, post_row


FIXTURE = Path(__file__).parent / 'fixtures' / 'mixed_board.html'
BASE = ['preview', '--gallery', 'fixture', '--account', 'synthetic', '--all-on-page']


class PagePreviewTests(TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.forbidden = [self.stack.enter_context(patch(name)) for name in (
            'rectifier.__main__.read_password', 'rectifier.auth.login')]

    def tearDown(self):
        for mocked in self.forbidden:
            mocked.assert_not_called()

    def run_cli(self, args):
        output, error = StringIO(), StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            code = main(args)
        return code, output.getvalue(), error.getvalue()

    def test_local_mixed_page_reports_all_and_preserves_members(self):
        with patch('rectifier.__main__.read_public_board') as read:
            code, output, _ = self.run_cli(BASE + ['--html', str(FIXTURE)])
            read.assert_not_called()
        data = json.loads(output)
        self.assertEqual(code, 0)
        self.assertEqual(data['observed_count'], 6)
        self.assertEqual(data['counts'], {'delete_guest': 2, 'protected_member': 2,
                                         'unknown_author': 2, 'conflicting_metadata': 0})
        self.assertEqual([x['post_id'] for x in data['items']], [106, 105, 104, 103, 102, 101])
        self.assertEqual(data['delete_requests'], 0)
        self.assertFalse(data['live_delete_available'])
        for private in ('192.0', 'synthetic_fixed', 'synthetic_semi', '합성 이름'):
            self.assertNotIn(private, output)

    def test_large_page_does_not_increase_execution_limit(self):
        html = board(*(post_row(number=str(n), link=f'/mgallery/board/view/?id=fixture&amp;no={n}')
                       for n in range(100, 160)))
        with patch('rectifier.__main__.read_public_board', return_value=html) as read:
            code, output, _ = self.run_cli(BASE + ['--page', '1'])
            read.assert_called_once_with('fixture', 1)
        self.assertEqual(code, 0)
        data = json.loads(output)
        self.assertEqual(data['target_count'], 60)
        self.assertEqual(data['page'], 1)
        self.assertNotIn('plan', data)
        self.assertEqual(MAX_TARGETS, 3)
        with self.assertRaises(ValueError):
            build_plan('fixture', 'synthetic', [100, 101, 102, 103], parse_board(html, 'fixture'))

    def test_empty_verified_table_is_zero_candidates(self):
        with patch('rectifier.__main__.read_public_board', return_value=board()):
            code, output, _ = self.run_cli(BASE + ['--page', '1'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)['observed_count'], 0)

    def test_duplicate_or_wrong_gallery_or_unrecognized_html_stops(self):
        bad_pages = [board(post_row(), post_row()),
                     board(post_row(link='/mgallery/board/view/?id=other&amp;no=101')),
                     '<html>login required</html>']
        for html in bad_pages:
            with self.subTest(html=html), patch('rectifier.__main__.read_public_board', return_value=html):
                code, output, error = self.run_cli(BASE + ['--page', '1'])
                self.assertEqual(code, 2)
                self.assertEqual(output, '')
                self.assertIn('중지', error)

    def test_network_error_is_not_retried_or_reported_as_empty(self):
        with patch('rectifier.__main__.read_public_board', side_effect=PublicReadError('HTTP 500')) as read:
            code, output, error = self.run_cli(BASE + ['--page', '1'])
            read.assert_called_once()
        self.assertEqual(code, 2)
        self.assertEqual(output, '')
        self.assertIn('HTTP 500', error)

    def test_invalid_selection_and_execute_option_never_read(self):
        for args in [BASE + ['--page', '1', '--ids', '101'],
                     BASE + ['--page', '1', '--execute'],
                     ['preview', '--gallery', 'fixture', '--account', 'synthetic', '--page', '1']]:
            with self.subTest(args=args), patch('rectifier.__main__.read_public_board') as read:
                with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                    main(args)
                read.assert_not_called()

    def test_bad_scope_rejected_before_read(self):
        for gallery, account in [('bad/gallery', 'synthetic'), ('fixture', ' ')]:
            with self.subTest(gallery=gallery), patch('rectifier.__main__.read_public_board') as read:
                code, output, _ = self.run_cli(['preview', '--gallery', gallery, '--account', account,
                                               '--all-on-page', '--page', '1'])
                self.assertEqual(code, 2)
                self.assertEqual(output, '')
                read.assert_not_called()

    def test_shared_policy_preserves_order_and_conflict_exclusion(self):
        posts = parse_board(FIXTURE.read_text(encoding='utf-8'), 'fixture')
        items = preview_page_items('fixture', iter(posts))
        for item in items:
            self.assertEqual(item, build_plan('fixture', 'synthetic', [item.post_id], posts).items[0])
        conflict = replace(posts[0], author_kind=AuthorKind.MEMBER, uid='synthetic_member')
        self.assertEqual(preview_page_items('fixture', [posts[0], conflict])[0].disposition,
                         'conflicting_metadata')
        with self.assertRaises(ValueError):
            preview_page_items('other', posts)

    def test_fixed_id_preview_remains_compatible(self):
        code, output, _ = self.run_cli(['preview', '--gallery', 'fixture', '--account', 'synthetic',
                                      '--ids', '106', '105', '999', '--html', str(FIXTURE)])
        data = json.loads(output)
        self.assertEqual(code, 0)
        self.assertEqual(data['target_count'], 1)
        self.assertEqual([x['disposition'] for x in data['plan']['items']],
                         ['delete_guest', 'protected_member', 'not_observed'])
