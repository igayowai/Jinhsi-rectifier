"""Multiple target integration with synthetic sessions and a virtual clock only."""
from unittest import TestCase
from unittest.mock import Mock, patch

from rectifier.auth import AuthStatus, LoginResult
from rectifier.bump import BumpLimits, BumpRunner, BumpTarget, MultiBumpAdapter, validate_bump_targets
from rectifier.bump_task import BumpTask
from tests.test_bump import ACCOUNT, TARGET, VIEW, Clock, Cancel, FakeSession, response
from tests import test_bump_ui as ui_fixtures
from tests import test_bump_controls as login_fixtures


TARGETS = tuple(BumpTarget(TARGET.gallery, TARGET.post_id + i) for i in range(20))


class MultiSession(FakeSession):
    def __init__(self, clock):
        super().__init__(clock)
        self.invalid = None

    def request(self, method, url, **kwargs):
        if method == 'GET' and '/view/' in url:
            self.calls.append((method, url))
            if url == self.invalid:
                return response('')
            number = url.split('no=')[1]
            return response(VIEW.replace(str(TARGET.post_id), number))
        return super().request(method, url, **kwargs)


class MultiTests(TestCase):
    def setUp(self):
        for name in ('socket.socket', 'rectifier.credentials._backend'):
            guard = patch(name, side_effect=AssertionError('offline only'))
            guard.start()
            self.addCleanup(guard.stop)
        self.clock = Clock()
        self.cancel = Cancel(self.clock)
        self.session = MultiSession(self.clock)
        self.login = LoginResult(AuthStatus.VERIFIED, 'synthetic', self.session, ACCOUNT)

    def run_multi(self, count=5, requests=12, interval=.5):
        adapter = MultiBumpAdapter(self.login, TARGETS[:count], ACCOUNT, clock=self.clock)
        runner = BumpRunner(adapter, BumpLimits(interval, 300, requests),
                            clock=self.clock, cancel=self.cancel)
        return runner.run(), adapter

    def test_five_posts_wrap_in_order_with_one_global_rate(self):
        stats, adapter = self.run_multi()
        ids = [dict(p[2]['form'])['nos[]'] for p in self.session.posts]
        self.assertEqual(ids, [str(TARGETS[i % 5].post_id) for i in range(12)])
        self.assertEqual([p[0] for p in self.session.posts], [i * .5 for i in range(12)])
        self.assertEqual(adapter.progress.acknowledged, (3, 3, 2, 2, 2))
        self.assertEqual(stats.stopped, 'request_limit')
        first_post = next(i for i, call in enumerate(self.session.calls) if call[0] == 'POST')
        for target in TARGETS[:5]:
            self.assertIn(('GET', target.url), self.session.calls[:first_post])

    def test_twenty_posts_at_lowest_ui_rate_complete_one_cycle(self):
        stats, adapter = self.run_multi(count=20, requests=20, interval=10)
        self.assertEqual(stats.requests, 20)
        self.assertEqual(adapter.progress.acknowledged, (1,) * 20)
        self.assertEqual(self.session.posts[-1][0], 190)

    def test_last_target_invalid_stops_before_any_post(self):
        self.session.invalid = TARGETS[19].url
        stats, _ = self.run_multi(count=20)
        self.assertEqual((stats.requests, stats.stopped), (0, 'target_unverified'))
        self.assertFalse(self.session.posts)

    def test_third_failure_stops_all_without_advancing(self):
        def fail_third():
            if len(self.session.posts) == 2:
                self.session.reply = response('{}')
        self.session.before_post = fail_third
        stats, adapter = self.run_multi()
        self.assertEqual((stats.requests, stats.unknown, stats.stopped), (3, 1, 'response_unknown'))
        self.assertEqual(adapter.progress.acknowledged, (1, 1, 0, 0, 0))
        self.assertEqual(adapter.progress.last_index, 2)

    def test_cancel_between_posts_stops_entire_set(self):
        self.cancel.on_wait = self.cancel.set
        stats, _ = self.run_multi()
        self.assertEqual((stats.requests, stats.stopped), (1, 'user_stopped'))

    def test_invalid_sets_rejected_without_io(self):
        for targets in ((), TARGETS[:1], (*TARGETS, BumpTarget(TARGET.gallery, 999)),
                        (*TARGETS[:4], TARGET), (*TARGETS[:4], BumpTarget('other', 123)),
                        (TARGET, 'bad')):
            with self.subTest(targets=targets), self.assertRaises(ValueError):
                validate_bump_targets(targets)
        self.assertFalse(self.session.calls)

    def test_worker_uses_one_login_and_reports_all_five_targets(self):
        login = Mock(return_value=self.login)
        task = BumpTask(TARGETS[0], ACCOUNT, BumpLimits(.5, 60, 5), password='synthetic',
                        second_target=TARGETS[1], additional_targets=TARGETS[2:5], login_fn=login)
        self.addCleanup(task.stop)
        task.start()
        task._thread.join(4)
        self.assertFalse(task.alive)
        self.assertEqual(task.pair_progress.acknowledged, (1,) * 5)
        self.assertEqual(task.targets, TARGETS[:5])
        login.assert_called_once()
        self.assertTrue(self.session.closed)


class MultiWindowTests(TestCase):
    # Reuse the existing inert UI fixture, without inheriting its test cases.
    setUp = ui_fixtures.BumpWindowTests.setUp
    tearDown = ui_fixtures.BumpWindowTests.tearDown
    poll = ui_fixtures.BumpWindowTests.poll

    def fill(self, count=5):
        self.ui.toggle_experiment()
        self.ui.second_url.set(TARGETS[1].url)
        self.ui.additional_urls.insert('1.0', '\n'.join(t.url for t in TARGETS[2:count]))

    def test_five_target_trial_freezes_order_and_renders_counts(self):
        from rectifier.bump import BumpPairProgress, BumpStats
        from rectifier.defense_task import TaskState
        self.fill()
        self.ui.start(trial=True)
        task = self.ui.task
        self.assertEqual(task.targets, TARGETS[:5])
        self.assertEqual(task.limits.max_requests, 5)
        self.assertEqual(str(self.ui.additional_urls['state']), 'disabled')
        self.assertEqual(len(self.ui.target_rows.get_children()), 5)
        task.pair_progress = BumpPairProgress(('test',) * 5, (1,) * 5, 4)
        task.state = TaskState('finished', BumpStats(5, 5), 'request_limit')
        task.alive = False
        self.poll()
        self.assertIn('최근 요청 5번', self.ui.counts.get())
        self.assertEqual(str(self.ui.target_rows.item('4')['values'][2]), '1')

    def test_repeat_cap_remains_global_for_twenty_targets(self):
        self.fill(20)
        self.ui.minutes.set('300')
        self.ui.rate.set('2')
        self.ui.start()
        self.assertEqual(len(self.ui.task.targets), 20)
        self.assertEqual(self.ui.task.limits.max_requests, 36000)

    def test_low_rate_trial_has_time_for_twenty_requests(self):
        self.fill(20)
        self.ui.rate.set('.1')
        self.ui.start(trial=True)
        self.assertGreaterEqual(self.ui.task.limits.duration, 204)
        self.assertEqual(self.ui.task.limits.max_requests, 20)

    def test_duplicate_or_twenty_first_url_never_starts(self):
        self.fill(20)
        self.ui.additional_urls.insert('end', '\n' + TARGET.url)
        self.ui.start()
        self.factory.assert_not_called()
        self.ui.additional_urls.delete('1.0', 'end')
        self.ui.additional_urls.insert('1.0', TARGET.url)
        self.ui.start()
        self.factory.assert_not_called()

    def test_return_to_single_ignores_hidden_extra_urls(self):
        self.fill()
        self.ui.toggle_experiment()
        self.ui.start(trial=True)
        self.assertEqual(self.ui.task.targets, (TARGET,))


class MultiLoginTests(TestCase):
    setUp = login_fixtures.AutoLoginTests.setUp
    tearDown = login_fixtures.AutoLoginTests.tearDown
    open = login_fixtures.AutoLoginTests.open
    login_factory = login_fixtures.AutoLoginTests.login_factory
    bump_factory = login_fixtures.AutoLoginTests.bump_factory
    pump_until = login_fixtures.AutoLoginTests.pump_until
    finish_bump = login_fixtures.AutoLoginTests.finish_bump

    def test_pending_login_keeps_all_five_targets_and_borrows_session(self):
        self.session = MultiSession(Clock())
        self.login.return_value = LoginResult(AuthStatus.VERIFIED, 'synthetic', self.session, ACCOUNT)
        self.ui.toggle_experiment()
        self.ui.second_url.set(TARGETS[1].url)
        self.ui.additional_urls.insert('1.0', '\n'.join(t.url for t in TARGETS[2:5]))
        self.ui.account.set(ACCOUNT)
        self.ui.password.set('synthetic')
        self.ui.rate.set('2')
        self.ui.start(trial=True)
        self.finish_bump()
        self.assertEqual(self.ui.task.targets, TARGETS[:5])
        self.assertEqual(self.ui.task.pair_progress.acknowledged, (1,) * 5)
        self.login.assert_called_once()
        self.bump_login.assert_not_called()
        self.assertFalse(self.session.closed)
