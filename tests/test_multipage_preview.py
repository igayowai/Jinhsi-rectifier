from contextlib import ExitStack, redirect_stdout, redirect_stderr
from io import StringIO
import json
from unittest import TestCase
from unittest.mock import Mock, call, patch

from rectifier.__main__ import main
from rectifier.page_preview import preview_pages
from rectifier.public_board import PublicReadError
from tests.test_parser import board, post_row


def row(n, writer='data-uid="" data-ip="192.0"'):
    return post_row(writer, number=str(n),
                    link=f'/mgallery/board/view/?id=fixture&amp;no={n}')


class MultiPagePreviewTests(TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('socket.socket', side_effect=AssertionError('offline test')))
        self.forbidden = [self.stack.enter_context(patch(name, side_effect=AssertionError(name)))
                          for name in ('rectifier.__main__.read_password', 'rectifier.auth.login')]

    def tearDown(self):
        for method in self.forbidden:
            method.assert_not_called()

    def cli(self, extra):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(['preview', '--gallery', 'fixture', '--account', 'synthetic'] + extra)
        return code, out.getvalue(), err.getvalue()

    def test_merge_preserves_order_deduplicates_and_excludes_conflicts(self):
        reader = Mock(side_effect=[board(row(8), row(7), row(6)),
                                   board(row(7), row(6, 'data-uid="member"'), row(5))])
        r = preview_pages('fixture', 'synthetic', [2, 4], reader=reader)
        self.assertEqual(reader.call_args_list, [call('fixture', 2), call('fixture', 4)])
        self.assertEqual([i['post_id'] for i in r['items']], [8, 7, 6, 5])
        self.assertEqual(r['counts']['delete_guest'], 3)
        self.assertEqual(r['counts']['conflicting_metadata'], 1)
        self.assertEqual(r['observed_count'], 4)
        self.assertEqual(r['observed_row_count'], 6)
        self.assertEqual(r['duplicate_observations'], 2)
        self.assertTrue(r['requested_pages_completed'])
        self.assertEqual(r['coverage'], 'not_guaranteed')
        for value in ('192.0', 'data-uid', 'synthetic nickname'):
            self.assertNotIn(value, json.dumps(r))
        self.assertNotIn('plan', r)
        self.assertFalse(r['live_delete_available'])

    def test_member_unknown_and_empty_pages(self):
        reader = Mock(side_effect=[board(row(3, 'data-uid="fixed"'),
                                         row(2, 'data-uid="semi"'), row(1, '')),
                                   board()])
        r = preview_pages('fixture', 'synthetic', [1, 2], reader=reader)
        self.assertEqual(r['counts']['protected_member'], 2)
        self.assertEqual(r['counts']['unknown_author'], 1)
        self.assertEqual(r['target_count'], 0)
        self.assertTrue(r['requested_pages_completed'])
        self.assertEqual(r['page_results'][1]['row_count'], 0)

    def test_network_failure_retains_partial_and_never_requests_rest(self):
        reader = Mock(side_effect=[board(row(1)), PublicReadError('private upstream text')])
        r = preview_pages('fixture', 'synthetic', [1, 2, 3], reader=reader)
        self.assertEqual(reader.call_count, 2)
        self.assertEqual([p['status'] for p in r['page_results']], ['read', 'failed', 'not_requested'])
        self.assertEqual(r['observed_count'], 1)
        self.assertFalse(r['requested_pages_completed'])
        self.assertEqual(r['stopped'], 'public_read_failed')
        self.assertNotIn('private upstream text', json.dumps(r))

    def test_failed_first_page_is_not_successful_empty_result(self):
        reader = Mock(side_effect=PublicReadError('HTTP 429'))
        r = preview_pages('fixture', 'synthetic', [1, 2], reader=reader)
        reader.assert_called_once_with('fixture', 1)
        self.assertFalse(r['requested_pages_completed'])
        self.assertEqual(r['observed_count'], 0)

    def test_malformed_page_and_wrong_gallery_reject_whole_page(self):
        for html in (board(row(1), row(1)), '<html>login</html>',
                     board(row(2).replace('id=fixture', 'id=other'))):
            with self.subTest(html=html):
                reader = Mock(side_effect=[board(row(3)), html])
                r = preview_pages('fixture', 'synthetic', [1, 2, 3], reader=reader)
                self.assertEqual(r['stopped'], 'invalid_board')
                self.assertEqual(r['observed_count'], 1)
                self.assertEqual(reader.call_count, 2)

    def test_validate_entire_scope_before_first_read(self):
        for pages in ([], [1, 2, 3, 4, 5, 6], [1, 1], [1, 0], [1, True], [1, 1_000_001]):
            with self.subTest(pages=pages):
                reader = Mock()
                with self.assertRaises(ValueError):
                    preview_pages('fixture', 'synthetic', pages, reader=reader)
                reader.assert_not_called()
        for gallery, account in [('bad/path', 'synthetic'), ('fixture', ' ')]:
            reader = Mock()
            with self.assertRaises(ValueError):
                preview_pages(gallery, account, [1], reader=reader)
            reader.assert_not_called()

    def test_cli_success_and_partial_failure_exit_codes(self):
        for replies, expected in [([board(row(1)), board(row(2))], 0),
                                  ([board(row(1)), PublicReadError('HTTP 500')], 2)]:
            with self.subTest(expected=expected), patch('rectifier.page_preview.read_public_board',
                                                       side_effect=replies) as reader:
                code, out, _ = self.cli(['--all-on-page', '--pages', '1', '2'])
                self.assertEqual(code, expected)
                data = json.loads(out)
                self.assertEqual(data['delete_requests'], 0)
                self.assertEqual(data['requested_pages_completed'], expected == 0)
                self.assertEqual(reader.call_count, 2)

    def test_cli_rejects_conflicting_selection_or_sources_without_reading(self):
        for extra in (['--ids', '1', '--pages', '1', '2'],
                      ['--all-on-page', '--pages', '1', '--page', '2'],
                      ['--all-on-page', '--pages', '1', '--html', 'x'],
                      ['--all-on-page', '--pages', '1', '--execute']):
            with self.subTest(extra=extra), patch('rectifier.page_preview.read_public_board') as reader:
                try:
                    code, _, _ = self.cli(extra)
                except SystemExit as error:
                    code = error.code
                self.assertEqual(code, 2)
                reader.assert_not_called()

    def test_unexpected_bug_is_not_disguised_as_read_failure(self):
        reader = Mock(side_effect=RuntimeError('bug'))
        with self.assertRaises(RuntimeError):
            preview_pages('fixture', 'synthetic', [1], reader=reader)
        self.assertEqual(reader.call_count, 1)
