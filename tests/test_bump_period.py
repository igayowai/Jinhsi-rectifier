"""Minute-period tests using synthetic sessions and virtual time, never live requests."""
from unittest import TestCase
from unittest.mock import patch

from rectifier.auth import AuthStatus, LoginResult
from rectifier.bump import BumpAdapter, BumpLimits, BumpRunner
from tests import test_bump_ui as fixtures
from tests.test_bump import ACCOUNT, TARGET, Clock, Cancel, FakeSession
from tests.test_bump_multi import TARGETS


class PeriodRunnerTests(TestCase):
    def test_all_minute_presets_refresh_before_each_send(self):
        with patch('socket.socket', side_effect=AssertionError('offline only')):
            for interval in (60, 120, 300, 600, 1200, 1800):
                with self.subTest(interval=interval):
                    clock = Clock()
                    session = FakeSession(clock)
                    result = LoginResult(AuthStatus.VERIFIED, 'synthetic', session, ACCOUNT)
                    adapter = BumpAdapter(result, TARGET, ACCOUNT, clock=clock)
                    runner = BumpRunner(adapter, BumpLimits(interval, interval * 3, 3),
                                        clock=clock, cancel=Cancel(clock))
                    stats = runner.run()
                    self.assertEqual([p[0] for p in session.posts], [0, interval, interval * 2])
                    self.assertEqual(session.calls.count(('GET', TARGET.url)), 3)
                    self.assertEqual(stats.stopped, 'request_limit')

    def test_stop_interrupts_thirty_minute_wait_without_extra_requests(self):
        clock = Clock()
        cancel = Cancel(clock)
        cancel.on_wait = cancel.set
        session = FakeSession(clock)
        adapter = BumpAdapter(LoginResult(AuthStatus.VERIFIED, 'synthetic', session, ACCOUNT),
                              TARGET, ACCOUNT, clock=clock)
        stats = BumpRunner(adapter, BumpLimits(1800, 18000, 10), clock=clock, cancel=cancel).run()
        self.assertEqual((stats.requests, stats.stopped), (1, 'user_stopped'))

    def test_duration_ends_during_long_wait_without_sending_again(self):
        clock = Clock()
        session = FakeSession(clock)
        adapter = BumpAdapter(LoginResult(AuthStatus.VERIFIED, 'synthetic', session, ACCOUNT),
                              TARGET, ACCOUNT, clock=clock)
        stats = BumpRunner(adapter, BumpLimits(1800, 600, 10), clock=clock, cancel=Cancel(clock)).run()
        self.assertEqual((stats.requests, stats.stopped, stats.elapsed), (1, 'duration_limit', 600))

    def test_intervals_above_thirty_minutes_remain_invalid(self):
        with self.assertRaises(ValueError):
            BumpLimits(interval=1801)


class PeriodWindowTests(TestCase):
    setUp = fixtures.BumpWindowTests.setUp
    tearDown = fixtures.BumpWindowTests.tearDown
    poll = fixtures.BumpWindowTests.poll

    def test_presets_ignore_unused_rate_and_have_correct_five_hour_caps(self):
        self.ui.minutes.set('300')
        self.ui.rate.set('unused')
        for name, interval, count in (('1분마다', 60, 300), ('2분마다', 120, 150), ('5분마다', 300, 60),
                                     ('10분마다', 600, 30), ('20분마다', 1200, 15), ('30분마다', 1800, 10)):
            self.assertIn(name, self.ui.period_entry['values'])
            self.ui.period.set(name)
            self.ui._period_changed()
            self.assertEqual(str(self.ui.rate_entry['state']), 'disabled')
            self.ui.start()
            task = self.ui.task
            self.assertEqual((task.limits.interval, task.limits.max_requests), (interval, count))
            self.assertEqual(str(self.ui.period_entry['state']), 'disabled')
            task.alive = False

    def test_twenty_target_trial_allows_a_full_five_minute_cycle(self):
        self.ui.toggle_experiment()
        self.ui.second_url.set(TARGETS[1].url)
        self.ui.additional_urls.insert('1.0', '\n'.join(t.url for t in TARGETS[2:]))
        self.ui.period.set('5분마다')
        self.ui.start(trial=True)
        limits = self.ui.task.limits
        self.assertEqual((limits.interval, limits.max_requests), (300, 20))
        self.assertGreater(limits.duration, 19 * 300)
        self.assertLessEqual(limits.duration, 18000)

    def test_large_slow_trial_explains_limit_but_repeat_still_starts(self):
        self.ui.toggle_experiment()
        self.ui.second_url.set(TARGETS[1].url)
        self.ui.additional_urls.insert('1.0', '\n'.join(t.url for t in TARGETS[2:]))
        for period in ('20분마다', '30분마다'):
            self.ui.period.set(period)
            self.ui.start(trial=True)
            self.factory.assert_not_called()
            self.assertIn('5시간 넘게', self.ui.status.get())
        self.ui.minutes.set('300')
        self.ui.start()
        self.assertEqual((self.ui.task.limits.interval, self.ui.task.limits.max_requests), (1800, 10))

    def test_ten_target_thirty_minute_trial_fits_five_hours(self):
        self.ui.toggle_experiment()
        self.ui.second_url.set(TARGETS[1].url)
        self.ui.additional_urls.insert('1.0', '\n'.join(t.url for t in TARGETS[2:10]))
        self.ui.period.set('30분마다')
        self.ui.start(trial=True)
        self.assertEqual(self.ui.task.limits.max_requests, 10)
        self.assertLessEqual(self.ui.task.limits.duration, 18000)

    def test_short_run_still_allows_first_immediate_request(self):
        self.ui.period.set('5분마다')
        self.ui.minutes.set('1')
        self.ui.start()
        self.assertEqual(self.ui.task.limits.max_requests, 1)

    def test_invalid_period_rejected_and_switch_back_unlocks_rate(self):
        self.ui.period.set('invalid')
        self.ui.start()
        self.factory.assert_not_called()
        self.ui.period.set('5분마다')
        self.ui._enabled(True)
        self.assertEqual(str(self.ui.period_entry['state']), 'readonly')
        self.assertEqual(str(self.ui.rate_entry['state']), 'disabled')
        self.ui.period.set('초당 횟수')
        self.ui._period_changed()
        self.assertEqual(str(self.ui.rate_entry['state']), 'normal')
