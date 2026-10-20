from unittest import TestCase

from rectifier.parser import BoardParseError, parse_board_entries, parse_board_listing
from tests.common import listing
from tests.test_parser import board, post_row


class ListingSnapshotTests(TestCase):
    def test_existing_reject_contract_and_entry_results_are_preserved(self):
        html = listing((101, 102))
        entries, excluded = parse_board_listing(html, 'mnt')
        self.assertEqual(entries, parse_board_entries(html, 'mnt'))
        self.assertEqual(excluded, frozenset())
        notice = listing((101,), kind='icon_notice')
        entries, excluded = parse_board_listing(notice, 'mnt')
        self.assertEqual(entries, ())
        self.assertEqual(excluded, frozenset({'101'}))
        for ids in ((101,), ('101',)):
            with self.assertRaises(BoardParseError):
                parse_board_entries(notice, 'mnt', reject_special_ids=ids)
        self.assertEqual(parse_board_entries(notice, 'mnt', reject_special_ids=(102,)), ())

    def test_all_excluded_row_types_keep_the_conflict_signal(self):
        for kind in ('icon_notice', 'icon_ad', 'icon_survey', 'icon_vote'):
            with self.subTest(kind=kind):
                html = board(post_row(number='101', attrs=f'data-no="101" data-type="{kind}"'))
                self.assertEqual(parse_board_listing(html, 'fixture'), ((), frozenset({'101'})))

    def test_excluded_ids_use_exact_original_strings(self):
        html = board(post_row(number='공지', attrs='data-no="0101" data-type="icon_notice"'))
        self.assertEqual(parse_board_entries(html, 'fixture', reject_special_ids=(101,)), ())
        with self.assertRaises(BoardParseError):
            parse_board_entries(html, 'fixture', reject_special_ids=('0101',))
