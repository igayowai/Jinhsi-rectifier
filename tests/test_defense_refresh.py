from unittest import TestCase

from rectifier.batch_journal import BatchResponse
from rectifier.defense_flow import CandidateBatch, RefreshingDefenseFlow
from tests.test_defense_flow import guest


class RefreshingDefenseTests(TestCase):
    def setUp(self):
        self.now = 0.0

    def flow(self, **kwargs):
        return RefreshingDefenseFlow('fixture', clock=lambda: self.now, **kwargs)

    def test_expiry_requires_fresh_observation_not_merely_time_passing(self):
        flow = self.flow()
        flow.observe([guest(1)])
        batch = flow.take_batch()
        flow.record_response(batch, BatchResponse.SUCCESS_REPORTED)
        self.now = 1.99
        flow.observe([guest(1)])
        self.assertIsNone(flow.take_batch())
        self.now = 2.0
        self.assertIsNone(flow.take_batch())
        flow.observe([guest(1)])
        self.assertEqual(flow.take_batch().post_ids, (1,))
        self.assertEqual(flow.stats.attempted, 2)

    def test_slow_inflight_request_cannot_expire_and_cooldown_starts_after_response(self):
        flow = self.flow()
        flow.observe([guest(1)])
        batch = flow.take_batch()
        self.now = 100
        flow.observe([guest(1), guest(2)])
        self.assertIsNone(flow.take_batch())
        flow.record_response(batch, BatchResponse.UNKNOWN)
        self.assertIsNone(flow.take_batch())
        self.now = 101
        flow.observe([guest(1), guest(2)])
        self.assertEqual(flow.take_batch().post_ids, (2,))

    def test_old_unissued_candidates_require_new_snapshot(self):
        flow = self.flow(batch_size=1)
        flow.observe([guest(1), guest(2)])
        batch = flow.take_batch()
        flow.record_response(batch, BatchResponse.SUCCESS_REPORTED)
        self.assertIsNone(flow.take_batch())
        flow.observe([guest(3)], replace_pending=False)
        self.assertEqual(flow.take_batch().post_ids, (3,))

    def test_capacity_does_not_evict_unexpired_ids(self):
        flow = self.flow(batch_size=1, tracked_limit=1)
        flow.observe([guest(1)])
        flow.record_response(flow.take_batch(), BatchResponse.UNKNOWN)
        flow.observe([guest(2)])
        self.assertIsNone(flow.take_batch())
        self.assertEqual(flow.stats.stopped, 'tracking_capacity')

    def test_long_run_does_not_hit_lifetime_tracking_limit(self):
        flow = self.flow(batch_size=50, tracked_limit=100)
        for start in range(1, 100051, 50):
            flow.observe([guest(pid) for pid in range(start, start+50)])
            batch = flow.take_batch()
            self.assertIsNotNone(batch)
            flow.record_response(batch, BatchResponse.SUCCESS_REPORTED)
            self.now += 2
            self.assertLessEqual(len(flow._attempted), 50)
            self.assertLessEqual(len(flow._expires), 50)
        self.assertEqual(flow.stats.attempted, 100050)
        self.assertIsNone(flow.stats.stopped)

    def test_duplicate_or_forged_receipt_stops(self):
        for forged in (True, False):
            flow = self.flow()
            flow.observe([guest(1)])
            batch = flow.take_batch()
            if not forged:
                flow.record_response(batch, BatchResponse.UNKNOWN)
            else:
                batch = CandidateBatch('fixture', (1,))
            with self.assertRaises(ValueError):
                flow.record_response(batch, BatchResponse.SUCCESS_REPORTED)
            self.assertEqual(flow.stats.stopped, 'response_mismatch')

    def test_late_unknown_never_restarts_after_user_stop(self):
        flow = self.flow()
        flow.observe([guest(1)])
        batch = flow.take_batch()
        flow.stop()
        flow.record_response(batch, BatchResponse.UNKNOWN)
        self.now = 3
        flow.observe([guest(1), guest(2)])
        self.assertIsNone(flow.take_batch())
        self.assertEqual(flow.stats.unknown_responses, 1)
        self.assertEqual(flow.stats.stopped, 'user_stopped')
