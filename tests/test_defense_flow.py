from dataclasses import replace
from unittest import TestCase
from unittest.mock import patch

from rectifier.batch_journal import BatchResponse
from rectifier.defense_flow import CandidateBatch, DefenseFlow
from rectifier.models import AuthorKind, Post
from rectifier.parser import ListEntry, PostKind, parse_board_entries
from tests.test_list_kind import row
from tests.test_parser import board


def guest(pid, **kwargs):
    return ListEntry(Post('fixture', pid, 'same title', AuthorKind.GUEST),
                     kwargs.get('kind', PostKind.ORDINARY))


class DefenseFlowTests(TestCase):
    def setUp(self):
        self.now = 0.0
        self.network = patch('socket.socket', side_effect=AssertionError('network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def flow(self, **kwargs):
        return DefenseFlow('fixture', clock=lambda: self.now, **kwargs)

    def test_acknowledgement_advances_without_claiming_deleted(self):
        flow = self.flow(batch_size=2)
        flow.observe([guest(1), guest(2), guest(3)])
        first = flow.take_batch()
        self.assertEqual(first.post_ids, (1, 2))
        self.assertIsNone(flow.take_batch())
        flow.record_response(first, BatchResponse.SUCCESS_REPORTED)
        self.assertEqual(flow.take_batch().post_ids, (3,))
        self.assertEqual(flow.stats.accepted_unverified, 2)
        self.assertEqual(flow.stats.confirmed_deleted, 0)

    def test_real_parser_filters_members_unknown_recommended_and_notice(self):
        entries = parse_board_entries(board(
            row(9), row(8, writer='data-uid="fixed"'),
            row(7, writer='data-uid="semi"'), row(6, writer=''),
            row(5, 'icon_recomimg'), row(4, 'icon_notice'), row(3, None)), 'fixture')
        flow = self.flow()
        flow.observe(entries)
        self.assertEqual(flow.take_batch().post_ids, (9,))

    def test_repeated_pages_and_identical_titles_never_repeat_ids(self):
        flow = self.flow(batch_size=2)
        flow.observe([guest(1), guest(1), guest(2)])
        first = flow.take_batch()
        flow.observe([guest(1), guest(2), guest(3)])
        flow.record_response(first, BatchResponse.SUCCESS_REPORTED)
        second = flow.take_batch()
        self.assertEqual(second.post_ids, (3,))
        flow.record_response(second, BatchResponse.SUCCESS_REPORTED)
        flow.observe([guest(1), guest(2), guest(3)])
        self.assertIsNone(flow.take_batch())
        self.assertEqual(flow.stats.attempted, 3)

    def test_queued_guest_changed_to_member_or_conflict_is_removed(self):
        for observations in ([guest(1, kind=PostKind.RECOMMENDED)],
                             [ListEntry(Post('fixture', 1, 'same title', AuthorKind.MEMBER, 'semi'), PostKind.ORDINARY)],
                             [guest(1), replace(guest(1), post=replace(guest(1).post, title='changed'))]):
            with self.subTest(observations=observations):
                flow = self.flow()
                flow.observe([guest(1), guest(2)])
                flow.observe(observations)
                self.assertEqual(flow.take_batch().post_ids, (2,))

    def test_rejected_unknown_and_technical_stops_never_issue_next(self):
        for response in (BatchResponse.UNKNOWN, BatchResponse.REJECTION_REPORTED):
            flow = self.flow(batch_size=1)
            flow.observe([guest(1), guest(2)])
            flow.record_response(flow.take_batch(), response)
            self.assertIsNone(flow.take_batch())
            self.assertEqual(flow.observe([guest(3)]), 0)
            self.assertEqual(flow.stats.accepted_unverified, 0)
        for reason in ('auth_required', 'forbidden', 'rate_limited', 'quota_exhausted',
                       'read_failed', 'storage_failed'):
            flow = self.flow()
            flow.observe([guest(1)])
            flow.stop(reason)
            self.assertIsNone(flow.take_batch())
            self.assertEqual(flow.stats.stopped, reason)

    def test_stop_during_request_accepts_late_receipt_but_never_restarts(self):
        flow = self.flow(batch_size=1)
        flow.observe([guest(1), guest(2)])
        batch = flow.take_batch()
        flow.stop()
        flow.record_response(batch, BatchResponse.SUCCESS_REPORTED)
        self.assertEqual(flow.stats.accepted_unverified, 1)
        self.assertEqual(flow.stats.stopped, 'user_stopped')
        self.assertIsNone(flow.take_batch())

    def test_foreign_duplicate_and_malformed_receipts_fail_closed(self):
        for mode in ('foreign', 'duplicate', 'malformed'):
            flow = self.flow(batch_size=1)
            flow.observe([guest(1), guest(2)])
            batch = flow.take_batch()
            candidate, response = batch, BatchResponse.SUCCESS_REPORTED
            if mode == 'foreign':
                candidate = CandidateBatch(batch.gallery, batch.post_ids)
            elif mode == 'duplicate':
                flow.record_response(batch, response)
            else:
                response = 'success_reported'
            with self.assertRaises(ValueError):
                flow.record_response(candidate, response)
            self.assertIsNone(flow.take_batch())

    def test_stale_candidates_stop_and_reobservation_refreshes(self):
        flow = self.flow()
        flow.observe([guest(1)])
        self.now = 4
        self.assertIsNone(flow.take_batch())
        self.assertEqual(flow.stats.stopped, 'stale_listing')
        flow = self.flow()
        flow.observe([guest(1), guest(2)])
        self.now = 6
        flow.observe([guest(2), guest(1)])
        self.now = 8
        self.assertEqual(flow.take_batch().post_ids, (1, 2))

    def test_capacity_never_evicts_requested_ids_or_silently_continues(self):
        flow = self.flow(batch_size=1, queue_limit=2)
        flow.observe([guest(1), guest(2)])
        self.assertEqual(flow.observe([guest(3)]), 0)
        self.assertEqual(flow.stats.queued, 2)
        self.assertEqual(flow.stats.stopped, 'queue_capacity')
        flow = self.flow(batch_size=1, tracked_limit=1)
        flow.observe([guest(1), guest(2)])
        flow.record_response(flow.take_batch(), BatchResponse.SUCCESS_REPORTED)
        self.assertIsNone(flow.take_batch())
        self.assertEqual(flow.stats.stopped, 'tracking_capacity')
        self.assertEqual(flow.stats.attempted, 1)

    def test_invalid_list_or_cross_gallery_stops_without_candidates(self):
        for entries in ([guest(1), object()], [replace(guest(1), post=replace(guest(1).post, gallery_id='other'))],
                        [guest(1)] * 1251):
            flow = self.flow()
            with self.assertRaises(ValueError):
                flow.observe(entries)
            self.assertIsNone(flow.take_batch())
            self.assertEqual(flow.stats.stopped, 'invalid_listing')

    def test_40_per_second_virtual_arrivals_without_any_confirmation_callback(self):
        flow = self.flow()
        observed, batches = [], []
        # Virtual 250ms ticks, ten arrivals per tick: this is NOT a speed test.
        for tick in range(40):
            self.now = tick / 4
            new_ids = range(tick * 10 + 1, tick * 10 + 11)
            observed.extend(new_ids)
            flow.observe([guest(pid) for pid in reversed(observed[-50:])])
            batch = flow.take_batch()
            batches.extend(batch.post_ids)
            flow.record_response(batch, BatchResponse.SUCCESS_REPORTED)
        self.assertEqual(set(batches), set(range(1, 401)))
        self.assertEqual(len(batches), 400)
        self.assertEqual(flow.stats.accepted_unverified, 400)
        self.assertEqual(flow.stats.confirmed_deleted, 0)
        self.assertEqual(flow.stats.queued, 0)
        self.assertIsNone(flow.stats.stopped)

    def test_invalid_bounds(self):
        for kwargs in ({'batch_size': True}, {'batch_size': 251}, {'queue_limit': 1},
                       {'tracked_limit': 0}, {'max_age': float('nan')}, {'max_age': True}):
            with self.assertRaises(ValueError):
                self.flow(**kwargs)
