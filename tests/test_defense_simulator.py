from threading import Thread
from unittest import TestCase
from unittest.mock import patch

from tests.defense_simulator import SyntheticBoard, make_runner
from rectifier.defense_runner import ListDefenseAdapter


class DynamicDefenseTests(TestCase):
    def setUp(self):
        denied = patch('socket.socket',side_effect=AssertionError('offline only'))
        denied.start()
        self.addCleanup(denied.stop)

    def server(self, **kwargs):
        return SyntheticBoard(rate=0,initial=20,pages=2,list_delay=.001,
                              account_delay=.001,per_post_delay=.001,
                              base_delete_delay=.01,**kwargs)

    def test_real_parser_protects_member_and_notice_and_drains(self):
        s = self.server()
        r = make_runner(s,interval=.25)
        stats = r.run(lambda _: r.request_stop() if len(s.deleted) == 20 else None)
        self.assertEqual(len(s.deleted),20)
        self.assertEqual(stats.accepted_unverified,20)
        self.assertEqual(s.unsafe,0)
        self.assertEqual(s.duplicates,0)
        self.assertEqual(s.max_active,2)

    def test_stale_successful_rows_not_resent_during_cooldown(self):
        s = self.server(ghost_seconds=1.)
        r = make_runner(s,interval=.25)
        r.run(lambda stats: r.request_stop() if stats.scans >= 4 else None)
        self.assertEqual(len(s.calls),2)
        self.assertEqual(s.already_deleted,0)

    def test_unknown_but_applied_is_not_blindly_resent(self):
        s = self.server(fault='unknown_applied',ghost_seconds=0)
        r = make_runner(s,interval=.25)
        stats = r.run(lambda stats: r.request_stop() if stats.scans >= 3 else None)
        self.assertEqual(stats.unknown_responses,1)
        self.assertEqual(len(s.deleted),20)
        self.assertEqual(len(s.calls),2)
        self.assertEqual(s.already_deleted,0)

    def test_unknown_unapplied_is_reobserved_after_cooldown(self):
        s = self.server(fault='unknown_unapplied',ghost_seconds=0)
        r = make_runner(s,interval=.25,cooldown=.25)
        stats = r.run(lambda stats: r.request_stop() if len(s.deleted)==20 or stats.scans>=8 else None)
        self.assertEqual(len(s.deleted),20)
        self.assertEqual(stats.unknown_responses,1)
        self.assertEqual(s.duplicates,0)
        first = s.calls[0]
        later = [p for p in s.calls[1:] if set(p['ids']).intersection(first['ids'])]
        self.assertTrue(later)
        self.assertTrue(all(p['start']-first['end'] >= .25 for p in later))

    def test_rate_limit_stops_and_joins_sibling(self):
        s = self.server(fault='rate_limited')
        r = make_runner(s,interval=.25)
        stats = r.run()
        self.assertEqual(stats.stopped,'rate_limited')
        self.assertLessEqual(len(s.calls),2)
        self.assertEqual(s.active,0)
        self.assertEqual(s.unsafe,0)

    def test_stop_during_posts_drains_and_does_not_start_next_wave(self):
        s = self.server()
        s.base_delete_delay = .12
        r = make_runner(s,interval=.25)
        t = Thread(target=r.run)
        t.start()
        try:
            self.assertTrue(s.post_entered.wait(2))
            r.request_stop()
        finally:
            r.request_stop()
            t.join(3)
        self.assertFalse(t.is_alive())
        self.assertEqual(s.active,0)
        self.assertLessEqual(len(s.calls),2)
        self.assertIsNone(r._reader_pool)

    def test_page_shift_changes_rows_without_losing_identity(self):
        now = [0.]
        s = SyntheticBoard(rate=40,duration=2,initial=60,pages=2,
                           clock=lambda:now[0],wait=lambda _:None)
        r = make_runner(s)
        snap1 = r.adapter.read_snapshot(r.cancel)
        now[0] = 1.
        snap2 = r.adapter.read_snapshot(r.cancel)
        self.assertGreater(len(snap2.candidates-snap1.candidates),0)
        self.assertGreater(len(snap1.candidates-snap2.candidates),0)
        self.assertLessEqual(len(snap2.candidates),100)

    def test_simulator_has_no_transport_fallback(self):
        with self.assertRaises(AssertionError):
            self.server().request('GET','https://example.com/')
