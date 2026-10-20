"""Offline integration: synthetic accounts, temporary preferences, no real vault/network."""
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic
import tkinter as tk
from unittest import TestCase
from unittest.mock import Mock, patch

from rectifier.account_profiles import AccountProfiles
from rectifier.account_task import LoginTask
from rectifier.auth import AuthStatus, LoginResult
from rectifier.bump import AlternatingBumpAdapter, BumpAdapter, BumpLimits, BumpRunner
from rectifier.bump_task import BumpTask
from rectifier.bump_ui import BumpWindow
from tests.test_bump import ACCOUNT, TARGET, Cancel, Clock, FakeSession, response
from tests.test_bump_experiment import SECOND, PairSession


class TimingTests(TestCase):
    def setUp(self):
        for name in ('socket.socket', 'rectifier.credentials._backend'):
            guard = patch(name, side_effect=AssertionError('offline only'))
            guard.start()
            self.addCleanup(guard.stop)
        self.clock = Clock()
        self.cancel = Cancel(self.clock)
        self.session = PairSession(self.clock)
        self.login = LoginResult(AuthStatus.VERIFIED, 'synthetic', self.session, ACCOUNT)

    def runner(self, *, pair=False, interval=.5, duration=5, count=4, jitter=None):
        adapter = (AlternatingBumpAdapter(self.login, TARGET, SECOND, ACCOUNT, clock=self.clock)
                   if pair else BumpAdapter(self.login, TARGET, ACCOUNT, clock=self.clock))
        return BumpRunner(adapter, BumpLimits(interval, duration, count, .2),
                          clock=self.clock, cancel=self.cancel,
                          random_uniform=jitter or (lambda low, high: high))

    def test_delay_is_added_never_subtracted_and_first_request_is_delayed(self):
        draws = Mock(side_effect=[.2, 0, .1, .2, 0])
        stats = self.runner(jitter=draws).run()
        starts = [p[0] for p in self.session.posts]
        for actual, expected in zip(starts, [.2, .7, 1.3, 2.0]):
            self.assertAlmostEqual(actual, expected)
        self.assertEqual(stats.requests, 4)
        self.assertTrue(all(call.args == (0, .2) for call in draws.call_args_list))

    def test_slow_response_still_has_extra_delay_and_no_catchup(self):
        self.session.latency = 1.4
        stats = self.runner(duration=6).run()
        for actual, expected in zip([p[0] for p in self.session.posts], [.2, 1.8, 3.4, 5]):
            self.assertAlmostEqual(actual, expected)
        self.assertEqual(stats.requests, 4)

    def test_cancel_during_first_random_wait_sends_nothing(self):
        self.cancel.on_wait = self.cancel.set
        stats = self.runner().run()
        self.assertEqual((stats.requests, stats.stopped), (0, 'user_stopped'))
        self.assertFalse(self.session.calls)

    def test_error_stops_without_next_random_delay_or_second_target(self):
        self.session.reply = response('{}')
        draws = Mock(return_value=.1)
        stats = self.runner(pair=True, jitter=draws).run()
        self.assertEqual((stats.requests, stats.unknown, stats.stopped), (1, 1, 'response_unknown'))
        draws.assert_called_once()

    def test_five_hour_pair_uses_one_global_clock_and_obeys_deadline(self):
        runner = self.runner(pair=True, duration=18000, count=36000)
        stats = runner.run()
        self.assertEqual(stats.stopped, 'duration_limit')
        self.assertAlmostEqual(stats.elapsed, 18000)
        self.assertGreater(stats.requests, 25000)
        self.assertLess(stats.requests, 36000)
        starts = [p[0] for p in self.session.posts]
        self.assertLess(starts[-1], 18000)
        self.assertTrue(all(b - a >= .7 - 1e-9 for a, b in zip(starts, starts[1:])))
        ids = [dict(p[2]['form'])['nos[]'] for p in self.session.posts]
        self.assertTrue(all(value == str((TARGET, SECOND)[i % 2].post_id) for i, value in enumerate(ids)))
        self.assertEqual(sum(runner.adapter.progress.acknowledged), stats.requests)

    def test_five_hour_maximum_without_jitter_does_not_keep_old_two_hour_cap(self):
        stats = self.runner(duration=18000, count=36000, jitter=lambda a, b: 0).run()
        self.assertEqual((stats.requests, stats.stopped), (36000, 'request_limit'))
        self.assertEqual(self.session.posts[-1][0], 17999.5)


class AutoLoginTests(TestCase):
    def setUp(self):
        for name in ('socket.socket', 'rectifier.credentials._backend'):
            guard = patch(name, side_effect=AssertionError('offline only'))
            guard.start()
            self.addCleanup(guard.stop)
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = {}
        self.writer = Mock(side_effect=lambda account, secret: self.vault.__setitem__(account, secret))
        self.reader = Mock(side_effect=lambda account: self.vault.get(account))
        self.profiles = AccountProfiles(Path(self.tmp.name) / 'bump-accounts.sqlite', password_writer=self.writer)
        self.session = FakeSession(Clock())
        self.login = Mock(return_value=LoginResult(AuthStatus.VERIFIED, 'synthetic', self.session, ACCOUNT))
        self.bump_login = Mock(side_effect=AssertionError('must reuse verified session'))
        self.root = tk.Tk()
        self.root.withdraw()
        self.ui = None
        self.open()

    def open(self):
        self.ui = BumpWindow(self.root, target_url=TARGET.url, profile_store=self.profiles,
                            login_task_factory=self.login_factory, task_factory=self.bump_factory)
        self.ui.window.withdraw()

    def login_factory(self, *args, **kwargs):
        return LoginTask(*args, login_fn=self.login, password_reader=self.reader, **kwargs)

    def bump_factory(self, *args, **kwargs):
        return BumpTask(*args, login_fn=self.bump_login, password_reader=self.reader, **kwargs)

    def pump_until(self, done):
        deadline = monotonic() + 4
        while not done() and monotonic() < deadline:
            self.root.update()
            Event().wait(.01)
        self.assertTrue(done(), 'synthetic UI worker did not finish')

    def tearDown(self):
        if self.ui is not None and not self.ui.closed:
            self.ui.close()
            self.pump_until(lambda: self.ui.closed)
        self.root.destroy()

    def sign_in(self, auto=True):
        self.ui.account.set(ACCOUNT)
        self.ui.password.set('synthetic-secret')
        self.ui.auto_login.set(auto)
        self.ui.sign_in()
        self.pump_until(lambda: self.ui.login_task is None)

    def finish_bump(self):
        self.pump_until(lambda: self.ui.task is not None and self.ui._finished_reported)

    def test_verified_login_saved_and_reopen_logs_in_without_bumping(self):
        self.sign_in()
        self.assertTrue(self.profiles.load().auto_login)
        self.assertEqual(self.ui.password.get(), '')
        self.assertIsNotNone(self.ui.login_result)
        self.assertFalse(self.session.posts)
        self.assertNotIn(b'synthetic-secret', self.profiles.path.read_bytes())
        self.ui.close()
        self.assertTrue(self.session.closed)
        self.session = FakeSession(Clock())
        self.login.return_value = LoginResult(AuthStatus.VERIFIED, 'synthetic', self.session, ACCOUNT)
        self.writer.reset_mock()
        self.open()
        self.assertEqual(self.ui.account.get(), ACCOUNT)
        self.pump_until(lambda: self.ui.login_result is not None)
        self.assertEqual(self.login.call_count, 2)
        self.assertIsNone(self.ui.task)
        self.assertFalse(self.session.posts)
        self.writer.assert_not_called()
        self.ui.start(trial=True)
        self.finish_bump()
        self.assertEqual(len(self.session.posts), 1)
        self.assertFalse(self.session.closed)
        self.bump_login.assert_not_called()
        self.assertEqual(self.login.call_count, 2)

    def test_start_also_verifies_and_saves_then_bumps_once(self):
        self.ui.account.set(ACCOUNT)
        self.ui.password.set('synthetic-secret')
        self.ui.auto_login.set(True)
        self.ui.start(trial=True)
        self.finish_bump()
        self.assertTrue(self.profiles.load().auto_login)
        self.assertEqual(self.ui.task.state.reason, 'request_limit')
        self.assertEqual(len(self.session.posts), 1)
        self.assertFalse(self.session.closed)
        self.login.assert_called_once()
        self.bump_login.assert_not_called()

    def test_stop_after_login_finishes_but_before_poll_does_not_bump(self):
        self.ui.account.set(ACCOUNT)
        self.ui.password.set('synthetic-secret')
        self.ui.start(trial=True)
        self.ui.login_task._thread.join(3)
        self.ui.stop()
        self.pump_until(lambda: self.ui.login_task is None)
        self.assertIsNone(self.ui.task)
        self.assertTrue(self.session.closed)
        self.assertFalse(self.session.posts)

    def test_close_during_login_waits_then_discards_result_without_saving(self):
        began, release = Event(), Event()
        result = self.login.return_value
        def blocked_login(*args, **kwargs):
            began.set()
            release.wait(3)
            return result
        self.login.side_effect = blocked_login
        self.ui.account.set(ACCOUNT)
        self.ui.password.set('synthetic-secret')
        self.ui.start(trial=True)
        self.assertTrue(began.wait(1))
        self.ui.close()
        self.assertFalse(self.ui.closed)
        self.assertFalse(self.session.closed)
        release.set()
        self.pump_until(lambda: self.ui.closed)
        self.assertTrue(self.session.closed)
        self.writer.assert_not_called()
        self.assertFalse(self.session.posts)

    def test_automatic_failure_disables_persisted_flag_and_never_retries(self):
        self.ui.close()
        self.profiles.remember(ACCOUNT, 'synthetic-secret', auto_login=True)
        self.login.return_value = LoginResult(AuthStatus.FAILED, 'synthetic-private', self.session)
        self.open()
        self.pump_until(lambda: self.login.called and self.ui.login_task is None)
        self.assertFalse(self.profiles.load().auto_login)
        self.assertFalse(self.ui.auto_login.get())
        self.assertIsNone(self.ui.auto_login_id)
        self.assertIsNone(self.ui.login_result)
        self.assertIsNone(self.ui.task)
        self.assertNotIn('synthetic-private', self.ui.login_status.get())
        self.ui.close()
        self.open()
        self.assertIsNone(self.ui.auto_login_id)
        self.login.assert_called_once()

    def test_wrong_account_is_never_saved_or_used_to_bump(self):
        self.login.return_value = LoginResult(AuthStatus.VERIFIED, 'synthetic', self.session, 'other')
        self.ui.account.set(ACCOUNT)
        self.ui.password.set('synthetic-secret')
        self.ui.auto_login.set(True)
        self.ui.start(trial=True)
        self.pump_until(lambda: self.ui.login_task is None)
        self.writer.assert_not_called()
        self.assertIsNone(self.ui.task)
        self.assertFalse(self.session.posts)
        self.assertTrue(self.session.closed)

    def test_profile_write_failure_keeps_current_login_usable(self):
        self.writer.side_effect = RuntimeError('synthetic-private')
        self.sign_in()
        self.assertIsNotNone(self.ui.login_result)
        self.assertIn('저장 실패', self.ui.credential_status.get())
        self.assertNotIn('synthetic-private', self.ui.credential_status.get())
        self.ui.start(trial=True)
        self.finish_bump()
        self.assertEqual(len(self.session.posts), 1)

    def test_corrupt_profile_does_not_prevent_manual_login_or_get_replaced(self):
        self.ui.close()
        self.profiles.path.write_bytes(b'synthetic-corrupt')
        self.open()
        self.assertIn('읽지 못함', self.ui.login_status.get())
        self.sign_in()
        self.assertIsNotNone(self.ui.login_result)
        self.assertEqual(self.profiles.path.read_bytes(), b'synthetic-corrupt')
        self.writer.assert_not_called()

    def test_account_edit_drops_cached_session_and_pending_startup_login(self):
        self.sign_in()
        self.ui.account.set('other')
        self.assertIsNone(self.ui.login_result)
        self.assertTrue(self.session.closed)
        self.ui.close()
        self.open()
        self.assertIsNotNone(self.ui.auto_login_id)
        self.ui.account.set('other')
        self.assertIsNone(self.ui.auto_login_id)

    def test_logout_disables_auto_and_closes_session(self):
        self.sign_in()
        self.ui.sign_out()
        self.assertTrue(self.session.closed)
        self.assertFalse(self.profiles.load().auto_login)
        self.ui.close()
        self.open()
        self.assertIsNone(self.ui.auto_login_id)

    def test_checkbox_updates_preference_without_resaving_password(self):
        self.sign_in(auto=False)
        self.writer.reset_mock()
        self.ui.auto_login_button.invoke()
        self.assertTrue(self.profiles.load().auto_login)
        self.ui.auto_login_button.invoke()
        self.assertFalse(self.profiles.load().auto_login)
        self.writer.assert_not_called()

    def test_forget_preserves_warning_if_auto_disable_cannot_be_saved(self):
        self.sign_in()
        self.ui.confirm_forget = Mock(return_value=True)
        self.ui.password_remover = Mock(return_value=True)
        with patch.object(self.profiles, 'disable_auto', side_effect=RuntimeError('synthetic-private')):
            self.ui.forget_saved_password()
        self.assertIn('해제 저장 실패', self.ui.credential_status.get())
        self.assertNotIn('synthetic-private', self.ui.credential_status.get())
        self.assertIsNone(self.ui.login_result)
        self.assertFalse(self.ui.auto_login.get())

    def test_error_closes_session_and_does_not_login_again(self):
        self.sign_in()
        self.session.reply = response('{}')
        self.ui.start(trial=True)
        self.finish_bump()
        self.assertEqual(self.ui.task.state.reason, 'response_unknown')
        self.assertTrue(self.session.closed)
        self.assertIsNone(self.ui.login_result)
        self.assertEqual(len(self.session.posts), 1)
        self.login.assert_called_once()
        self.bump_login.assert_not_called()

    def test_session_expiry_disables_auto_and_stops_before_post(self):
        self.sign_in()
        self.session.menu = ''
        self.ui.start(trial=True)
        self.finish_bump()
        self.assertEqual(self.ui.task.state.reason, 'auth_required')
        self.assertFalse(self.profiles.load().auto_login)
        self.assertFalse(self.session.posts)
        self.assertTrue(self.session.closed)

    def test_invalid_rate_or_duration_is_rejected_before_login(self):
        self.ui.account.set(ACCOUNT)
        for rate in ('0', '-1', '2.1', 'nan', 'inf', ''):
            self.ui.rate.set(rate)
            self.ui.start()
            self.assertIn('입력 확인', self.ui.status.get())
        self.ui.rate.set('2')
        self.ui.minutes.set('301')
        self.ui.start()
        self.login.assert_not_called()
        self.reader.assert_not_called()

    def test_controls_fit_both_modes_at_current_display_scale(self):
        self.ui.window.deiconify()
        for experimental in (False, True):
            if experimental:
                self.ui.toggle_experiment()
            self.root.update()
            top = self.ui.window.winfo_rooty()
            bottom = top + self.ui.window.winfo_height()
            controls = (*self.ui.inputs, self.ui.login_button, self.ui.auto_login_button,
                        self.ui.stop_button, self.ui.multi_mode_radio, self.ui.help_label)
            for widget in controls:
                if widget in (self.ui.second_url_entry, self.ui.additional_urls) and not experimental:
                    continue
                self.assertTrue(widget.winfo_ismapped(), str(widget))
            self.assertGreaterEqual(self.ui.stop_button.winfo_rooty(), top)
            self.assertLessEqual(self.ui.stop_button.winfo_rooty() + self.ui.stop_button.winfo_height(), bottom)
            self.ui.body_canvas.yview_moveto(1)
            self.root.update()
            self.assertLessEqual(self.ui.help_label.winfo_rooty() + self.ui.help_label.winfo_height(),
                                 self.ui.stop_button.winfo_rooty())
            self.ui.body_canvas.yview_moveto(0)

    def test_close_during_bump_keeps_borrowed_session_until_worker_finishes(self):
        self.sign_in()
        began, release = Event(), Event()
        def during_post():
            began.set()
            release.wait(3)
        self.session.before_post = during_post
        self.ui.start(trial=True)
        self.assertTrue(began.wait(1))
        self.ui.close()
        self.assertFalse(self.session.closed)
        self.assertFalse(self.ui.closed)
        release.set()
        self.pump_until(lambda: self.ui.closed)
        self.assertTrue(self.session.closed)
        self.assertEqual(len(self.session.posts), 1)

    def test_captcha_cancel_stops_pending_start_and_never_posts(self):
        def login(*args, **kwargs):
            self.assertIsNone(kwargs['captcha_prompt'](b'synthetic-image'))
            return LoginResult(AuthStatus.REJECTED, '사용자가 보안문자 입력을 취소했거나 시간이 초과됨',
                               self.session)
        self.login.side_effect = login
        self.ui.account.set(ACCOUNT)
        self.ui.password.set('synthetic-secret')
        self.ui.start(trial=True)
        with patch('rectifier.bump_ui.prompt_captcha', return_value=None) as prompt:
            self.pump_until(lambda: self.ui.login_task is None)
        prompt.assert_called_once()
        self.assertIsNone(self.ui.task)
        self.assertFalse(self.session.posts)
        self.assertTrue(self.session.closed)
        self.writer.assert_not_called()


    def test_five_hour_decimal_rate_and_experiment_share_global_limits(self):
        from tests.test_bump_ui import FakeTask
        def fake_factory(*args, **kwargs):
            kwargs.pop('login_result', None)
            return FakeTask(*args, **kwargs)
        self.ui.task_factory = fake_factory
        self.sign_in()
        self.ui.minutes.set('300')
        for rate, count, interval in (('2', 36000, .5), ('.5', 9000, 2), ('.1', 1800, 10)):
            self.ui.rate.set(rate)
            self.ui.start()
            task = self.ui.task
            self.assertEqual((task.limits.duration, task.limits.max_requests,
                              task.limits.interval, task.limits.jitter), (18000, count, interval, .2))
            task.alive = False
        self.ui.toggle_experiment()
        self.ui.second_url.set(SECOND.url)
        self.ui.rate.set('2')
        self.ui.start()
        self.assertEqual(self.ui.task.limits.max_requests, 36000)
        self.assertEqual(self.ui.task.limits.interval, .5)
        self.ui.task.alive = False
