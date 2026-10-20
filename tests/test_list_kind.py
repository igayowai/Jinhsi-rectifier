from contextlib import ExitStack, redirect_stdout, redirect_stderr
from dataclasses import asdict
from io import StringIO
import json
from unittest import TestCase
from unittest.mock import Mock, patch

from rectifier.__main__ import main
from rectifier.page_preview import classify_list_entries, preview_pages
from rectifier.parser import BoardParseError, parse_board, parse_board_entries
from rectifier.public_board import PublicReadError
from tests.test_parser import board, post_row


def row(n, kind='icon_txt', writer='data-uid="" data-ip="192.0"'):
    attrs = '' if kind is None else f'data-type="{kind}"'
    return post_row(writer, number=str(n), attrs=attrs,
                    link=f'/mgallery/board/view/?id=fixture&amp;no={n}')


class ListKindTests(TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name in ('socket.socket', 'rectifier.auth.login', 'rectifier.__main__.read_password'):
            self.stack.enter_context(patch(name, side_effect=AssertionError(name)))

    def classify(self, html):
        return classify_list_entries('fixture', parse_board_entries(html, 'fixture'))

    def cli(self, args):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(['preview', '--gallery', 'fixture', '--account', 'synthetic'] + args)
        return code, json.loads(out.getvalue())

    def test_guest_candidates_are_distinct_from_batch_candidates(self):
        r = self.classify(board(row(8), row(7, 'icon_pic'), row(6, 'icon_recomimg'),
                                row(5, None), row(4, 'unknown_future'), row(3, 'icon_notice')))
        self.assertEqual(r['target_count'], 5)
        self.assertEqual(r['batch_candidate_count'], 2)
        self.assertEqual([i['batch_disposition'] for i in r['items']],
                         ['candidate', 'candidate', 'single_only', 'unknown_post_kind', 'unknown_post_kind'])
        self.assertEqual([i['post_kind'] for i in r['items']],
                         ['ordinary', 'ordinary', 'recommended', 'unknown', 'unknown'])
        self.assertNotIn(3, [i['post_id'] for i in r['items']])

    def test_member_or_unknown_author_is_never_batch_candidate(self):
        for kind in ('icon_txt', 'icon_pic', 'icon_recomimg', None):
            with self.subTest(kind=kind):
                r = self.classify(board(row(3, kind, 'data-uid="fixed"'),
                                       row(2, kind, 'data-uid="semi"'), row(1, kind, '')))
                self.assertEqual(r['batch_candidate_count'], 0)
                self.assertEqual(r['batch_counts']['protected_member'], 2)
                self.assertEqual(r['batch_counts']['unknown_author'], 1)

    def test_kind_changes_across_pages_are_excluded_in_both_orders(self):
        for kinds in (('icon_txt', 'icon_recomimg'), ('icon_recomimg', 'icon_txt'),
                      ('icon_txt', None), (None, 'icon_txt')):
            with self.subTest(kinds=kinds):
                reader = Mock(side_effect=[board(row(1, k)) for k in kinds])
                r = preview_pages('fixture', 'synthetic', [1, 2], reader=reader)
                self.assertEqual(r['target_count'], 1)  # Legacy author classification.
                self.assertEqual(r['batch_candidate_count'], 0)
                self.assertEqual(r['items'][0]['post_kind'], 'conflicting')
                self.assertEqual(r['items'][0]['batch_disposition'], 'post_kind_conflict')
                self.assertEqual(r['duplicate_observations'], 1)

    def test_same_kind_duplicates_merge_and_metadata_conflict_still_wins(self):
        for writer, expected in [('data-uid="" data-ip="192.0"', 'candidate'),
                                 ('data-uid="member"', 'conflicting_metadata')]:
            with self.subTest(writer=writer):
                reader = Mock(side_effect=[board(row(1)), board(row(1, 'icon_pic', writer))])
                r = preview_pages('fixture', 'synthetic', [1, 2], reader=reader)
                self.assertEqual(r['items'][0]['batch_disposition'], expected)
                self.assertEqual(r['observed_count'], 1)

    def test_partial_failure_retains_kind_without_claiming_complete(self):
        reader = Mock(side_effect=[board(row(1, 'icon_recomimg')), PublicReadError('failure')])
        r = preview_pages('fixture', 'synthetic', [1, 2, 3], reader=reader)
        self.assertFalse(r['requested_pages_completed'])
        self.assertEqual(r['batch_counts']['single_only'], 1)
        self.assertEqual(r['batch_candidate_count'], 0)
        self.assertEqual(reader.call_count, 2)

    def test_single_and_multiple_page_cli_show_the_same_classification(self):
        html = board(row(2), row(1, 'icon_recomimg'))
        with patch('rectifier.__main__.read_public_board', return_value=html):
            code, single = self.cli(['--all-on-page', '--page', '1'])
        self.assertEqual(code, 0)
        with patch('rectifier.page_preview.read_public_board', return_value=html):
            code, multiple = self.cli(['--all-on-page', '--pages', '1'])
        self.assertEqual(code, 0)
        self.assertEqual(single['items'], multiple['items'])
        self.assertEqual(single['batch_counts'], multiple['batch_counts'])
        for r in (single, multiple):
            self.assertEqual(r['batch_candidate_count'], 1)
            self.assertFalse(r['live_delete_available'])
            self.assertEqual(r['delete_requests'], 0)
            self.assertNotIn('plan', r)
            for private in ('192.0', 'synthetic nickname', 'data-type'):
                self.assertNotIn(private, json.dumps(r))

    def test_legacy_post_and_fixed_id_plan_shapes_stay_unchanged(self):
        html = board(row(1, 'icon_recomimg'))
        post = parse_board(html, 'fixture')[0]
        self.assertEqual(set(asdict(post)), {'gallery_id', 'post_id', 'title', 'author_kind', 'uid'})
        self.assertEqual(post, parse_board_entries(html, 'fixture')[0].post)
        with patch('rectifier.__main__.read_public_board', return_value=html):
            code, r = self.cli(['--ids', '1', '--page', '1'])
        self.assertEqual(code, 0)
        self.assertEqual(set(r['plan']['items'][0]), {'post_id', 'disposition', 'title'})
        self.assertNotIn('batch_candidate_count', r)

    def test_missing_unknown_and_malformed_type_never_promote_to_ordinary(self):
        for kind in (None, '', 'ICON_TXT', 'icon_txt icon_recomimg', 'icon_movie', ' icon_txt'):
            with self.subTest(kind=kind):
                r = self.classify(board(row(1, kind)))
                self.assertEqual(r['batch_candidate_count'], 0)
        with self.assertRaises(BoardParseError):
            self.classify(board(row(1).replace('data-type="icon_txt"',
                                               'data-type="icon_txt" data-type="icon_recomimg"')))

    def test_empty_and_invalid_scope_keep_existing_safeguards(self):
        r = self.classify(board())
        self.assertEqual(r['batch_candidate_count'], 0)
        self.assertEqual(sum(r['batch_counts'].values()), 0)
        with self.assertRaises(ValueError):
            classify_list_entries('other', parse_board_entries(board(row(1)), 'fixture'))
        with self.assertRaises(ValueError):
            classify_list_entries('fixture', parse_board(board(row(1)), 'fixture'))
