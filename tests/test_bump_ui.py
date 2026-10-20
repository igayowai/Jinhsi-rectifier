from queue import Queue
from threading import Event
import tkinter as tk
from unittest import TestCase
from unittest.mock import Mock, patch

from rectifier.auth import AuthStatus, LoginResult
from rectifier.bump import BumpLimits, BumpPairProgress, BumpStats
from rectifier.bump_task import BumpTask
from rectifier.bump_ui import BumpWindow
from rectifier.credentials import read_password
from rectifier.defense_task import TaskState
from tests.test_bump import TARGET, ACCOUNT, Clock, FakeSession


class BumpTaskTests(TestCase):
    def setUp(self):
        blocked = patch('socket.socket', side_effect=AssertionError('offline only'))
        blocked.start()
        self.addCleanup(blocked.stop)
        self.session = FakeSession(Clock())
        self.login = Mock(return_value=LoginResult(AuthStatus.VERIFIED, 'synthetic', self.session, ACCOUNT))
        self.password = Mock(return_value='synthetic-test-password')

    def task(self, **kwargs):
        task = BumpTask(TARGET, ACCOUNT, BumpLimits(duration=60),
                        login_fn=self.login, password_reader=self.password, **kwargs)
        self.addCleanup(task.stop)
        return task

    def finish(self, task):
        task._thread.join(3)
        self.assertFalse(task.alive)
        self.assertEqual(task.state.stage, 'finished')

    def test_constructor_inert_and_worker_closes_own_session_without_db(self):
        task = self.task()
        self.password.assert_not_called()
        self.login.assert_not_called()
        with patch('sqlite3.connect', side_effect=AssertionError('no DB')):
            task.start()
            self.finish(task)
        self.assertEqual((task.state.stats.requests, task.state.reason), (1, 'request_limit'))
        self.assertTrue(self.session.closed)
        self.assertIsNone(task._password)
        self.assertNotIn('synthetic-test-password', repr(task.state))
        with self.assertRaises(RuntimeError):
            task.start()

    def test_typed_password_is_transient_and_does_not_read_store(self):
        task = self.task(password='synthetic-typed-password')
        task.start()
        self.finish(task)
        self.password.assert_not_called()
        self.login.assert_called_once()
        self.assertIsNone(task._password)
        self.assertTrue(self.session.closed)

    def test_missing_credentials_and_wrong_identity_never_post(self):
        self.password.return_value = None
        task = self.task()
        task.start()
        self.finish(task)
        self.assertEqual(task.state.reason, 'credential_missing')
        self.login.assert_not_called()
        self.password.return_value = 'synthetic'
        self.login.return_value = LoginResult(AuthStatus.VERIFIED, 'synthetic', self.session, 'other')
        task = self.task()
        task.start()
        self.finish(task)
        self.assertEqual(task.state.reason, 'auth_required')
        self.assertFalse(self.session.posts)
        self.assertTrue(self.session.closed)

    def test_cancel_before_start_avoids_credential_read(self):
        task = self.task(password='synthetic-transient')
        task.stop()
        task.start()
        self.finish(task)
        self.password.assert_not_called()
        self.login.assert_not_called()
        self.assertIsNone(task._password)
        self.assertEqual(task.state.reason, 'user_stopped')

    def test_cancel_during_login_closes_session_and_never_posts(self):
        began, release = Event(), Event()
        result = self.login.return_value
        def login(*args, **kwargs):
            began.set()
            release.wait(2)
            return result
        self.login.side_effect = login
        task = self.task()
        task.start()
        self.assertTrue(began.wait(2))
        task.stop()
        release.set()
        self.finish(task)
        self.assertEqual(task.state.reason, 'user_stopped')
        self.assertTrue(self.session.closed)
        self.assertFalse(self.session.posts)

    def test_worker_failure_does_not_expose_exception_or_retry(self):
        self.password.side_effect = RuntimeError('synthetic-private-error')
        task = self.task()
        task.start()
        self.finish(task)
        self.assertEqual(task.state.reason, 'worker_failed')
        self.assertNotIn('synthetic-private-error', repr(task.state))
        self.login.assert_not_called()

    def test_captcha_cancel_unblocks_worker(self):
        def login(*args, **kwargs):
            kwargs['captcha_prompt'](b'synthetic-image')
            return self.login.return_value
        self.login.side_effect = login
        task = self.task()
        task.start()
        request = task.captchas.get(timeout=2)
        task.stop()
        self.finish(task)
        self.assertEqual(request.image, b'')
        self.assertEqual(task.state.reason, 'user_stopped')
        self.assertFalse(self.session.posts)


class FakeTask:
    def __init__(self, target, account, limits, *, password=None, second_target=None, additional_targets=()):
        self.target, self.account, self.limits = target, account, limits
        self.second_target = second_target
        self.targets = (target,) if second_target is None else (target, second_target, *additional_targets)
        self.pair_progress = None
        self.alive = False
        self.starts = 0
        self.cancel = Event()
        self.captchas = Queue()
        self.title = ''
        self.state = TaskState()

    def start(self):
        self.starts += 1
        self.alive = True
        self.state = TaskState('checking')

    def stop(self):
        self.cancel.set()


class BumpWindowTests(TestCase):
    def setUp(self):
        block = patch('socket.socket', side_effect=AssertionError('offline only'))
        block.start()
        self.addCleanup(block.stop)
        self.root = tk.Tk()
        self.root.withdraw()
        self.closed = Mock()
        self.factory = Mock(side_effect=FakeTask)
        self.store = {}
        self.backend = Mock()
        self.backend.get_password.side_effect = lambda service, user: self.store.get((service, user))
        self.backend.set_password.side_effect = lambda service, user, value: self.store.__setitem__((service, user), value)
        self.backend.delete_password.side_effect = lambda service, user: self.store.pop((service, user), None)
        vault = patch('rectifier.credentials._backend', return_value=self.backend)
        self.vault_factory = vault.start()
        self.addCleanup(vault.stop)
        self.confirm = Mock(return_value=False)
        self.ui = BumpWindow(self.root, self.closed, account=ACCOUNT, target_url=TARGET.url,
                             task_factory=self.factory, confirm_forget=self.confirm)
        self.ui.window.withdraw()

    def tearDown(self):
        if not self.ui.closed:
            if self.ui.task:
                if isinstance(self.ui.task, FakeTask):
                    self.ui.task.alive = False
                else:
                    self.ui.task.stop()
                    self.ui.task._thread.join(3)
            self.ui.close()
        self.root.destroy()

    def poll(self):
        if self.ui.poll_id is not None:
            self.ui.window.after_cancel(self.ui.poll_id)
        self.ui._poll()

    def test_open_is_inert_and_trial_is_exactly_one_request(self):
        self.factory.assert_not_called()
        self.vault_factory.assert_not_called()
        self.ui.password.set('synthetic-ui-password')
        self.ui.start(trial=True)
        self.assertEqual(self.ui.task.limits.max_requests, 1)
        self.assertEqual(self.ui.password.get(), '')
        self.assertEqual(str(self.ui.url_entry['state']), 'disabled')
        self.ui.start()
        self.assertEqual(self.factory.call_count, 1)

    def test_duration_limit_and_invalid_url_never_construct_task(self):
        for minutes in ('0', '301', 'nan', '1.5'):
            self.ui.minutes.set(minutes)
            self.ui.start()
        self.ui.url.set('https://example.invalid/')
        self.ui.start(trial=True)
        self.factory.assert_not_called()

    def test_two_hour_ui_limits_and_close_waits_for_worker(self):
        self.ui.minutes.set('120')
        self.ui.start()
        self.assertEqual((self.ui.task.limits.duration, self.ui.task.limits.max_requests), (7200, 14400))
        self.ui.close()
        self.assertTrue(self.ui.task.cancel.is_set())
        self.assertFalse(self.ui.closed)
        self.closed.assert_not_called()
        self.ui.task.state = TaskState('finished', BumpStats(stopped='user_stopped'), 'user_stopped')
        self.ui.task.alive = False
        self.poll()
        self.assertTrue(self.ui.closed)
        self.closed.assert_called_once()

    def test_unknown_result_and_input_error_are_visible_without_auto_restart(self):
        self.ui.start(trial=True)
        self.ui.task.state = TaskState('finished', BumpStats(1, 0, 1, 1, 1000, 'response_unknown'), 'response_unknown')
        self.ui.task.alive = False
        self.poll()
        self.assertIn('미확정', self.ui.status.get())
        self.assertIn('미확정 1회', self.ui.counts.get())
        self.assertEqual(self.factory.call_count, 1)
        self.ui.url.set('invalid')
        self.ui.start()
        self.poll()
        self.assertIn('입력 확인', self.ui.status.get())

    def test_factory_error_does_not_echo_secrets_and_controls_recover(self):
        self.factory.side_effect = RuntimeError('synthetic-private-error')
        self.ui.start()
        self.assertNotIn('synthetic-private-error', self.ui.status.get())
        self.assertEqual(str(self.ui.start_button['state']), 'normal')

    def test_save_button_clears_input_and_next_start_uses_saved_path(self):
        self.ui.account.set('  Synthetic_User  ')
        self.ui.password.set('synthetic-saved-password')
        self.ui.save_password_button.invoke()
        self.backend.set_password.assert_called_once_with(
            'rectifier:dcinside:synthetic_user', 'synthetic_user', 'synthetic-saved-password')
        self.assertEqual(self.ui.password.get(), '')
        self.assertIn('저장 완료', self.ui.credential_status.get())
        self.factory.assert_not_called()
        self.ui.start(trial=True)
        self.assertIsNone(self.factory.call_args.kwargs['password'])
        self.assertEqual(self.ui.task.starts, 1)

    def test_saved_password_is_loaded_after_window_reopen_with_fake_login(self):
        self.ui.password.set('synthetic-saved-password')
        self.ui.save_password()
        self.ui.close()
        session = FakeSession(Clock())
        login = Mock(return_value=LoginResult(AuthStatus.VERIFIED, 'synthetic', session, ACCOUNT))
        def factory(target, account, limits, **kwargs):
            return BumpTask(target, account, limits, login_fn=login, password_reader=read_password, **kwargs)
        self.backend.reset_mock()
        self.ui = BumpWindow(self.root, account=ACCOUNT, target_url=TARGET.url,
                             task_factory=factory, confirm_forget=self.confirm)
        self.ui.window.withdraw()
        self.backend.get_password.assert_not_called()
        self.assertEqual(self.ui.password.get(), '')
        self.ui.start(trial=True)
        self.ui.task._thread.join(3)
        self.assertFalse(self.ui.task.alive)
        self.assertEqual(login.call_args.args[:2], (ACCOUNT, 'synthetic-saved-password'))
        self.assertEqual(self.ui.task.state.reason, 'request_limit')
        self.assertTrue(session.closed)
        self.backend.set_password.assert_not_called()

    def test_existing_password_is_preserved_and_replacement_is_not_automatic(self):
        key = ('rectifier:dcinside:' + ACCOUNT, ACCOUNT)
        self.store[key] = 'synthetic-old-password'
        self.ui.password.set('synthetic-new-password')
        self.ui.save_password()
        self.assertEqual(self.store[key], 'synthetic-old-password')
        self.backend.set_password.assert_not_called()
        self.backend.delete_password.assert_not_called()
        self.assertIn('덮어쓰지 않았음', self.ui.credential_status.get())
        self.assertEqual(self.ui.password.get(), 'synthetic-new-password')

    def test_invalid_id_or_empty_password_never_accesses_vault(self):
        self.ui.save_password()
        self.assertIn('비밀번호를 입력', self.ui.credential_status.get())
        self.ui.account.set('bad/id')
        self.ui.password.set('synthetic')
        self.ui.save_password()
        self.ui.forget_saved_password()
        self.assertIn('ID 형식', self.ui.credential_status.get())
        self.vault_factory.assert_not_called()
        self.confirm.assert_not_called()

    def test_forget_cancel_keeps_store_and_confirm_identifies_shared_account(self):
        self.ui.forget_password_button.invoke()
        self.confirm.assert_called_once()
        self.assertIn(ACCOUNT, self.confirm.call_args.args[1])
        self.assertIn('메인 정류기', self.confirm.call_args.args[1])
        self.assertEqual(self.confirm.call_args.kwargs['default'], 'no')
        self.vault_factory.assert_not_called()
        self.assertIn('취소', self.ui.credential_status.get())

    def test_confirmed_forget_only_removes_selected_account_and_can_save_again(self):
        selected = ('rectifier:dcinside:' + ACCOUNT, ACCOUNT)
        other = ('rectifier:dcinside:synthetic_other', 'synthetic_other')
        self.store.update({selected: 'synthetic-old', other: 'synthetic-other'})
        self.confirm.return_value = True
        self.ui.forget_password_button.invoke()
        self.backend.delete_password.assert_called_once_with(*selected)
        self.assertNotIn(selected, self.store)
        self.assertEqual(self.store[other], 'synthetic-other')
        self.assertIn('지움', self.ui.credential_status.get())
        self.ui.password.set('synthetic-new')
        self.ui.save_password()
        self.assertEqual(self.store[selected], 'synthetic-new')

    def test_forget_missing_entry_never_deletes(self):
        self.confirm.return_value = True
        self.ui.forget_saved_password()
        self.backend.delete_password.assert_not_called()
        self.assertIn('저장된 비밀번호 없음', self.ui.credential_status.get())

    def test_credential_controls_and_handlers_are_blocked_until_task_finishes(self):
        self.ui.start(trial=True)
        self.ui.password.set('synthetic')
        for button in (self.ui.save_password_button, self.ui.forget_password_button):
            self.assertEqual(str(button['state']), 'disabled')
            button.invoke()
        self.ui.save_password()
        self.ui.forget_saved_password()
        self.vault_factory.assert_not_called()
        self.confirm.assert_not_called()
        self.ui.task.alive = False
        self.ui.task.state = TaskState('finished', BumpStats(), 'request_limit')
        self.poll()
        self.assertEqual(str(self.ui.save_password_button['state']), 'normal')
        self.assertEqual(str(self.ui.forget_password_button['state']), 'normal')
        self.ui.save_password()
        self.poll()
        self.assertIn('저장 완료', self.ui.credential_status.get())

    def test_backend_failures_are_not_reported_as_success_or_secret_details(self):
        self.backend.get_password.side_effect = RuntimeError('synthetic-private-error')
        self.ui.password.set('synthetic')
        self.ui.save_password()
        self.assertIn('실패', self.ui.credential_status.get())
        self.assertNotIn('synthetic-private-error', self.ui.credential_status.get())
        self.confirm.return_value = True
        self.ui.forget_saved_password()
        self.assertIn('실패', self.ui.credential_status.get())
        self.assertNotIn('synthetic-private-error', self.ui.credential_status.get())
        self.backend.set_password.assert_not_called()
        self.backend.delete_password.assert_not_called()

    def test_generic_callback_errors_are_sanitized(self):
        self.ui.password_saver = Mock(side_effect=RuntimeError('synthetic-private-error'))
        self.ui.password.set('synthetic')
        self.ui.save_password()
        self.assertIn('실패', self.ui.credential_status.get())
        self.assertNotIn('synthetic-private-error', self.ui.credential_status.get())
        self.ui.password_remover = Mock(side_effect=RuntimeError('synthetic-private-error'))
        self.confirm.return_value = True
        self.ui.forget_saved_password()
        self.assertNotIn('synthetic-private-error', self.ui.credential_status.get())
        self.vault_factory.assert_not_called()

    def test_failed_save_verification_does_not_claim_success(self):
        self.backend.set_password.side_effect = None
        self.ui.password.set('synthetic')
        self.ui.save_password()
        self.assertIn('저장 결과 확인 실패', self.ui.credential_status.get())
        self.assertNotIn('저장 완료', self.ui.credential_status.get())
        self.assertEqual(self.ui.password.get(), 'synthetic')

    def test_multi_mode_radio_only_reveals_inputs(self):
        # One switch for single/multi: the radios at the top, no duplicate bottom button.
        self.assertFalse(self.ui.experiment_button.winfo_manager())
        self.ui.multi_mode_radio.invoke()
        self.assertTrue(self.ui.experimental)
        self.assertEqual(self.ui.experiment_box.winfo_manager(), 'pack')
        self.assertEqual(self.ui.trial_button['text'], '전체 1회씩')
        self.factory.assert_not_called()
        self.vault_factory.assert_not_called()

    def test_experiment_rejects_missing_duplicate_or_cross_gallery_urls_before_task(self):
        self.ui.toggle_experiment()
        for url in ('', TARGET.url, TARGET.url.replace('id=mnt', 'id=other')):
            self.ui.second_url.set(url)
            self.ui.start(trial=True)
            self.assertIn('서로 다른 글', self.ui.status.get())
        self.factory.assert_not_called()
        self.vault_factory.assert_not_called()

    def test_experiment_trial_sends_two_requests_at_global_one_second_interval(self):
        from tests.test_bump_experiment import SECOND
        self.ui.toggle_experiment()
        self.ui.second_url.set(SECOND.url)
        self.ui.start(trial=True)
        self.assertEqual(self.ui.task.second_target, SECOND)
        self.assertEqual((self.ui.task.limits.interval, self.ui.task.limits.max_requests), (1, 2))
        self.assertEqual(str(self.ui.experiment_button['state']), 'disabled')
        self.assertEqual(str(self.ui.second_url_entry['state']), 'disabled')
        self.ui.toggle_experiment()
        self.assertTrue(self.ui.experimental)
        self.ui.start()
        self.assertEqual(self.factory.call_count, 1)
        self.ui.stop_button.invoke()
        self.assertTrue(self.ui.task.cancel.is_set())

    def test_experiment_two_hour_limit_is_total_7200_requests(self):
        from tests.test_bump_experiment import SECOND
        self.ui.toggle_experiment()
        self.ui.second_url.set(SECOND.url)
        self.ui.minutes.set('120')
        self.ui.start()
        self.assertEqual((self.ui.task.limits.interval, self.ui.task.limits.duration,
                          self.ui.task.limits.max_requests), (1, 7200, 7200))

    def test_experiment_counts_remain_per_post_and_can_return_to_single_mode(self):
        from tests.test_bump_experiment import SECOND
        self.ui.toggle_experiment()
        self.ui.second_url.set(SECOND.url)
        self.ui.start(trial=True)
        self.ui.task.pair_progress = BumpPairProgress(('first', 'second'), (1, 0), 1)
        self.ui.task.state = TaskState('finished', BumpStats(2, 1, 1, 1, 1, 'response_unknown'), 'response_unknown')
        self.ui.task.alive = False
        self.poll()
        self.poll()
        self.assertIn('A 성공 1회 · B 성공 0회 · 최근 요청 B', self.ui.counts.get())
        self.assertEqual(self.ui.counts.get().count('A 성공'), 1)
        self.assertIn('미확정', self.ui.status.get())
        self.assertIn(str(SECOND.post_id), self.ui.target_label.get())
        self.ui.experiment_button.invoke()
        self.assertFalse(self.ui.experimental)
        self.assertEqual(self.ui.experiment_box.winfo_manager(), '')
        self.assertEqual(self.ui.trial_button['text'], '1회 시험')
        self.poll()
        self.assertNotIn('A 성공', self.ui.counts.get())
        self.ui.start(trial=True)
        self.assertIsNone(self.ui.task.second_target)
        self.assertEqual((self.ui.task.limits.interval, self.ui.task.limits.max_requests), (.5, 1))
