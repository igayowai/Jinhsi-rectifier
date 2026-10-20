"""Two-post scheduling checks using dummy pages, a fake clock and no network."""
from unittest import TestCase
from unittest.mock import Mock, patch

from rectifier.auth import AuthStatus, LoginResult
from rectifier.bump import (AlternatingBumpAdapter, BumpLimits, BumpRunner, BumpTarget,
                            validate_bump_pair)
from rectifier.bump_task import BumpTask
from tests.test_bump import ACCOUNT, TARGET, VIEW, SUCCESS, Cancel, Clock, FakeSession, response


SECOND = BumpTarget(TARGET.gallery, TARGET.post_id + 1)


class PairSession(FakeSession):
    def __init__(self, clock):
        super().__init__(clock)
        self.second_view = VIEW.replace(str(TARGET.post_id), str(SECOND.post_id)).replace(
            'synthetic notice text', 'synthetic second text')
        self.replies = []

    def request(self, method, url, **kwargs):
        if method == 'GET' and url == SECOND.url:
            self.calls.append((method, url))
            return response(self.second_view)
        if method == 'POST' and self.replies:
            self.reply = self.replies.pop(0)
        return super().request(method, url, **kwargs)


class BumpExperimentTests(TestCase):
    def setUp(self):
        for target in ('socket.socket', 'rectifier.credentials._backend'):
            guard = patch(target, side_effect=AssertionError('offline only'))
            guard.start()
            self.addCleanup(guard.stop)
        self.clock = Clock()
        self.cancel = Cancel(self.clock)
        self.session = PairSession(self.clock)
        self.login = LoginResult(AuthStatus.VERIFIED, 'synthetic', self.session, ACCOUNT)

    def run_pair(self, *, limits=None, progress=lambda value: None):
        adapter = AlternatingBumpAdapter(self.login, TARGET, SECOND, ACCOUNT, clock=self.clock)
        runner = BumpRunner(adapter, limits or BumpLimits(interval=1, duration=10, max_requests=6),
                            cancel=self.cancel, clock=self.clock)
        return runner.run(progress), adapter

    def posted_ids(self):
        return [dict(kwargs['form'])['nos[]'] for _, _, kwargs in self.session.posts]

    def test_exact_alternation_one_post_per_second_and_per_target_counts(self):
        stats, adapter = self.run_pair()
        self.assertEqual(self.posted_ids(), [str(TARGET.post_id), str(SECOND.post_id)] * 3)
        self.assertEqual([p[0] for p in self.session.posts], [0, 1, 2, 3, 4, 5])
        self.assertEqual((stats.requests, stats.acknowledged, stats.stopped), (6, 6, 'request_limit'))
        self.assertEqual(adapter.progress.acknowledged, (3, 3))
        self.assertEqual(adapter.progress.last_index, 1)
        self.assertEqual(adapter.progress.titles, ('synthetic notice text', 'synthetic second text'))
        first_post = next(i for i, (method, _) in enumerate(self.session.calls) if method == 'POST')
        self.assertIn(('GET', TARGET.url), self.session.calls[:first_post])
        self.assertIn(('GET', SECOND.url), self.session.calls[:first_post])
        for _, _, kwargs in self.session.posts:
            self.assertEqual(sum(key == 'nos[]' for key, _ in kwargs['form']), 1)

    def test_pair_validation_before_any_request(self):
        for second in (None, TARGET, BumpTarget('different', SECOND.post_id)):
            with self.subTest(second=second), self.assertRaises(ValueError):
                validate_bump_pair(TARGET, second)
        self.assertFalse(self.session.calls)

    def test_second_post_must_verify_before_first_post_is_sent(self):
        self.session.second_view = ''
        stats, adapter = self.run_pair()
        self.assertEqual((stats.requests, stats.stopped), (0, 'target_unverified'))
        self.assertEqual(adapter.progress.acknowledged, (0, 0))
        self.assertFalse(self.session.posts)

    def test_second_failure_halts_both_without_retrying_or_advancing(self):
        for failure, reason in ((response('{}'), 'response_unknown'),
                                (response(SUCCESS, 429), 'rate_limited'),
                                (response(SUCCESS, 403), 'forbidden'),
                                (response(SUCCESS, 401), 'auth_required'),
                                (TimeoutError('synthetic-private'), 'response_unknown')):
            with self.subTest(reason=reason):
                self.session.posts.clear()
                self.session.replies = [response(SUCCESS), failure]
                stats, adapter = self.run_pair()
                self.assertEqual(self.posted_ids(), [str(TARGET.post_id), str(SECOND.post_id)])
                self.assertEqual((stats.requests, stats.acknowledged, stats.stopped), (2, 1, reason))
                self.assertEqual(adapter.progress.acknowledged, (1, 0))
                self.assertEqual(adapter.progress.last_index, 1)
                self.assertEqual(stats.unknown, int(reason == 'response_unknown'))
                self.assertNotIn('synthetic-private', repr(stats))

    def test_first_failure_never_sends_second_post(self):
        self.session.reply = response('{}')
        stats, adapter = self.run_pair()
        self.assertEqual(self.posted_ids(), [str(TARGET.post_id)])
        self.assertEqual((stats.stopped, adapter.progress.last_index), ('response_unknown', 0))

    def test_cancel_during_wait_stops_before_second(self):
        self.cancel.on_wait = self.cancel.set
        stats, _ = self.run_pair()
        self.assertEqual((stats.requests, stats.stopped), (1, 'user_stopped'))

    def test_cancel_during_first_post_preserves_ack_but_stops_both(self):
        self.session.before_post = self.cancel.set
        stats, adapter = self.run_pair()
        self.assertEqual((stats.requests, stats.stopped), (1, 'user_stopped'))
        self.assertEqual(adapter.progress.acknowledged, (1, 0))

    def test_slow_network_does_not_catch_up_or_parallelize(self):
        self.session.latency = 1.4
        stats, _ = self.run_pair(limits=BumpLimits(interval=1, duration=4, max_requests=10))
        self.assertEqual([p[0] for p in self.session.posts], [0, 1.4, 2.8])
        self.assertEqual(stats.stopped, 'duration_limit')

    def test_actual_dispatch_delay_still_leaves_one_second_between_posts(self):
        def delayed_token(*args):
            self.clock.now += .3
            return 'synthetic-dummy-token'
        self.session.cookie_value = delayed_token
        self.run_pair()
        starts = [p[0] for p in self.session.posts]
        self.assertEqual(len(starts), 6)
        self.assertTrue(all(b - a >= 1 for a, b in zip(starts, starts[1:])))

    def test_second_target_identity_change_during_refresh_stops_before_next_post(self):
        def progress(value):
            if value.requests == 30:
                self.session.second_view = self.session.second_view.replace('synthetic second text', 'changed')
        stats, adapter = self.run_pair(limits=BumpLimits(interval=1, duration=90, max_requests=60),
                                       progress=progress)
        self.assertEqual((stats.requests, stats.stopped), (30, 'target_changed'))
        self.assertEqual(adapter.progress.acknowledged, (15, 15))

    def test_two_hour_global_limit_has_3600_requests_per_post(self):
        stats, adapter = self.run_pair(limits=BumpLimits(interval=1, duration=7200, max_requests=7200))
        self.assertEqual((stats.requests, stats.acknowledged), (7200, 7200))
        self.assertEqual(adapter.progress.acknowledged, (3600, 3600))
        self.assertEqual(self.session.posts[-1][0], 7199)
        self.assertEqual(self.session.calls.count(('GET', TARGET.url)), 240)
        self.assertEqual(self.session.calls.count(('GET', SECOND.url)), 240)

    def test_pair_uses_same_five_hour_global_limit_as_single_target(self):
        limits = BumpLimits(interval=.5, duration=18000, max_requests=36000, jitter=.2)
        task = BumpTask(TARGET, ACCOUNT, limits, second_target=SECOND)
        self.assertEqual(task.limits, limits)
        self.assertFalse(self.session.calls)

    def test_task_logs_in_once_shares_session_and_publishes_pair_progress(self):
        login = Mock(return_value=self.login)
        reader = Mock(return_value='synthetic-password')
        task = BumpTask(TARGET, ACCOUNT, BumpLimits(interval=1, duration=60, max_requests=2),
                        second_target=SECOND, login_fn=login, password_reader=reader)
        self.addCleanup(task.stop)
        login.assert_not_called()
        task.start()
        task._thread.join(3)
        self.assertFalse(task.alive)
        login.assert_called_once()
        reader.assert_called_once_with(ACCOUNT)
        self.assertEqual(task.pair_progress.acknowledged, (1, 1))
        self.assertEqual(task.state.reason, 'request_limit')
        self.assertEqual(self.posted_ids(), [str(TARGET.post_id), str(SECOND.post_id)])
        self.assertTrue(self.session.closed)
        self.assertIsNone(task._password)
