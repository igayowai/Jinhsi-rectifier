from pathlib import Path
from queue import Queue
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic, sleep
import tkinter as tk
from unittest import TestCase
from unittest.mock import Mock, patch

from rectifier.auth import AuthStatus, LoginResult
from rectifier.captcha_ui import prompt_captcha
from rectifier.defense_runner import DefenseRunStats
from rectifier.defense_task import CaptchaRequest, DefenseTask, TaskState
from tests.common import ACCOUNT
from rectifier.manager_ui import ManagerUI
from tests.test_captcha import GIF
from tests.common import listing
from tests.common import MENU, CONTROL, response
from rectifier.auth import DEFAULT_RETURN_URL


class DefenseTaskTests(TestCase):
    def test_task_rejects_old_or_discontinuous_defense_pages(self):
        for pages in ((1500,), (2, 3), (1, 3), (2, 1)):
            with self.subTest(pages=pages), self.assertRaises(ValueError):
                DefenseTask('mnt', pages, account=ACCOUNT)
        self.assertEqual(DefenseTask('mnt', (1, 2, 3), account=ACCOUNT).pages, (1, 2, 3))

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        blocked = patch('socket.socket', side_effect=AssertionError('network forbidden'))
        blocked.start()
        self.addCleanup(blocked.stop)
        self.password = Mock(return_value='synthetic-dummy')
        self.session = Mock()
        self.login = Mock(return_value=LoginResult(AuthStatus.VERIFIED, 'synthetic', self.session, ACCOUNT))
        # A pre-start row below the later scheduler fixtures establishes the boundary.
        self.session.request.side_effect = lambda method, url, **kwargs: response(
            MENU if url == DEFAULT_RETURN_URL else listing((1,)) + CONTROL)

    def task(self, **kwargs):
        task = DefenseTask('mnt', (1,), account=ACCOUNT, password_reader=self.password,
                           login_fn=self.login, **kwargs)
        self.addCleanup(task.stop)
        return task

    def wait(self, condition):
        deadline = monotonic()+3
        while not condition() and monotonic() < deadline:
            sleep(.01)
        self.assertTrue(condition())

    def test_opening_task_is_inert_and_run_never_touches_a_database(self):
        task = self.task()
        self.assertFalse((self.root / 'operations.sqlite').exists())
        self.password.assert_not_called()
        runner = Mock()
        runner.run.return_value = DefenseRunStats(0, 0, 0, 0, 0, 'user_stopped', 0)
        with patch('rectifier.defense_task.DefenseRunner', return_value=runner), patch(
                'sqlite3.connect', side_effect=AssertionError('DB forbidden')) as db:
            task.start()
            self.wait(lambda: not task.alive)
            db.assert_not_called()
        self.assertEqual(task.state.reason, 'user_stopped')
        self.password.assert_called_once()
        self.login.assert_called_once()
        self.assertFalse((self.root / 'operations.sqlite').exists())

    def test_worker_runs_on_owned_thread_closes_session_and_keeps_only_counts(self):
        final = DefenseRunStats(2, 1, 50, 0, 0, 'user_stopped', 12)
        runner = Mock()
        runner.stats = final
        runner.run.return_value = final
        with patch('rectifier.defense_task.DefenseRunner', return_value=runner) as runner_type:
            task = self.task()
            task.start()
            self.wait(lambda: not task.alive)
            self.assertTrue(runner_type.call_args.args[0].include_recommended)
        self.assertEqual(task.state.stats.requests, 1)
        self.assertNotIn('synthetic-dummy', repr(task.state))
        self.session.close.assert_called_once()
        with self.assertRaises(RuntimeError):
            task.start()

    def test_missing_password_and_login_failure_never_construct_runner(self):
        for missing in (True, False):
            with self.subTest(missing=missing), patch('rectifier.defense_task.DefenseRunner') as runner:
                self.password.return_value = None if missing else 'synthetic-dummy'
                self.login.return_value = LoginResult(AuthStatus.REJECTED, 'synthetic', self.session)
                task = self.task()
                task.start()
                self.wait(lambda: not task.alive)
                self.assertEqual(task.state.reason, 'credential_missing' if missing else 'auth_required')
                runner.assert_not_called()

    def test_baseline_failure_never_starts_runner(self):
        self.session.request.side_effect = lambda method, url, **kwargs: response(
            MENU if url == DEFAULT_RETURN_URL else '')
        with patch('rectifier.defense_task.DefenseRunner') as runner:
            task = self.task()
            task.start()
            self.wait(lambda: not task.alive)
            runner.assert_not_called()
        self.assertEqual(task.state.reason, 'empty_response')
        self.assertTrue(all(call.args[0] == 'GET' for call in self.session.request.call_args_list))

    def test_cancel_during_login_prevents_runner_and_closes_session(self):
        started, release = Event(), Event()
        result = self.login.return_value
        def slow(*args, **kwargs):
            started.set()
            release.wait(2)
            return result
        self.login.side_effect = slow
        with patch('rectifier.defense_task.DefenseRunner') as runner:
            task = self.task()
            task.start()
            self.assertTrue(started.wait(1))
            task.stop()
            release.set()
            self.wait(lambda: not task.alive)
            runner.assert_not_called()
            self.assertEqual(task.state.reason, 'user_stopped')
            self.session.close.assert_called_once()

    def test_captcha_bridge_returns_answer_and_clears_image_and_answer(self):
        observed = []
        def login(*args, captcha_prompt):
            observed.append(captcha_prompt(GIF))
            return LoginResult(AuthStatus.REJECTED, 'synthetic', self.session)
        self.login.side_effect = login
        task = self.task()
        task.start()
        request = task.captchas.get(timeout=2)
        self.assertNotIn(repr(GIF), repr(request))
        request.answer = 'a1b2'
        request.ready.set()
        self.wait(lambda: not task.alive)
        self.assertEqual(observed, ['a1b2'])
        self.assertEqual(request.image, b'')
        self.assertIsNone(request.answer)

    def test_cancel_during_captcha_unblocks_worker_without_ui(self):
        def login(*args, captcha_prompt):
            self.assertIsNone(captcha_prompt(GIF))
            return LoginResult(AuthStatus.REJECTED, 'synthetic', self.session)
        self.login.side_effect = login
        task = self.task()
        task.start()
        task.captchas.get(timeout=2)
        task.stop()
        self.wait(lambda: not task.alive)
        self.assertEqual(task.state.reason, 'user_stopped')

    def test_real_runner_stops_without_new_wave_or_files_and_closes(self):
        task = self.task()
        self.session.cookie_value.return_value = 'synthetic-token'
        first_page = True
        def request(method, url, **kwargs):
            nonlocal first_page
            if method == 'POST':
                task.stop()
                return response('{"result":"success"}')
            if url == DEFAULT_RETURN_URL:
                return response(MENU)
            body = listing((1,)) if first_page else listing()
            first_page = False
            return response(body + CONTROL)
        self.session.request.side_effect = request
        with patch('sqlite3.connect', side_effect=AssertionError('DB forbidden')) as db, patch(
                'pathlib.Path.mkdir', side_effect=AssertionError('mkdir forbidden')) as mkdir:
            task.start()
            self.wait(lambda: not task.alive)
            db.assert_not_called()
            mkdir.assert_not_called()
        self.assertEqual(task.state.stage, 'finished')
        posts = [call for call in self.session.request.call_args_list if call.args[0] == 'POST']
        sent_ids = [v for call in posts for k, v in call.kwargs['form'] if k == 'nos[]']
        self.assertIn(len(posts), (1, 2))
        self.assertEqual(len(set(sent_ids)), len(sent_ids))
        self.assertEqual(task.state.stats.requests, len(posts))
        self.assertEqual(task.state.stats.accepted_unverified, len(sent_ids))
        self.assertEqual(task.state.reason, 'user_stopped')
        self.session.close.assert_called_once()
        self.assertEqual(list(self.root.iterdir()), [])


class FakeTask:
    def __init__(self, gallery, pages, *, include_new_members=False):
        self.gallery, self.pages = gallery, pages
        self.include_new_members = include_new_members
        self.cancel = Event()
        self.captchas = Queue()
        self.state = TaskState()
        self.alive = False
        self.starts = 0

    def start(self):
        self.starts += 1
        self.alive = True
        self.state = TaskState('running', DefenseRunStats(1, 1, 2, 0, 0, None, 13))

    def stop(self):
        self.cancel.set()

    def finish(self):
        self.alive = False
        self.state = TaskState('finished', self.state.stats, 'user_stopped')


class DefenseUITests(TestCase):
    def test_new_member_option_is_off_by_default_and_never_remembered(self):
        window = self.window()
        self.assertFalse(window.include_new_members.get())
        self.assertFalse(hasattr(window, 'arm_box'))
        window.new_members_box.invoke()
        self.start(window)
        self.assertTrue(window.task.include_new_members)
        self.assertIn('disabled', window.new_members_box.state())
        self.tick(window)
        self.assertIn('신규 딱지 회원', window.status.get())
        window.task.finish()
        window.close()
        reopened = self.window()
        self.assertFalse(reopened.include_new_members.get())

    def test_scope_is_latest_and_selected_count_is_locked_at_start(self):
        self.ui.pages.set('1500')
        window = self.window()
        self.assertEqual(window.pages, (1,))
        window.page_count.set('3')
        self.start(window)
        self.assertEqual(window.task.pages, (1, 2, 3))
        self.assertIn('disabled', window.page_count_box.state())
        self.assertEqual(self.ui.pages.get(), '1500')

    def test_invalid_count_never_starts(self):
        window = self.window()
        window.page_count.set('1500')
        self.start(window)
        self.factory.assert_not_called()
        self.assertIsNone(window.task)

    def setUp(self):
        writer = patch('rectifier.defense_monitor.write_summary', return_value=True)
        writer.start()
        self.addCleanup(writer.stop)
        try:
            self.root = tk.Tk()
        except tk.TclError:
            self.skipTest('Tk display unavailable')
        self.root.withdraw()
        self.factory = Mock(side_effect=FakeTask)
        self.ui = ManagerUI(self.root, reader=Mock(), defense_task_factory=self.factory)
        self.ui.login_result = Mock(verified_user_id=ACCOUNT)
        self.ui._delete_state(None)
        self.ui.gallery.set('mnt')
        self.addCleanup(self.cleanup)

    def cleanup(self):
        window = self.ui.defense_window
        if window is not None and window.task is not None:
            window.task.finish()
        if not self.ui.closed:
            self.ui.close()

    def window(self):
        self.ui.show_defense()
        return self.ui.defense_window

    def tick(self, window):
        window.window.after_cancel(window.poll_id)
        window._poll()

    def start(self, window):
        window.start_button.invoke()

    def test_open_inert_explicit_start_locks_manual_and_prevents_duplicate_start(self):
        window = self.window()
        self.factory.assert_not_called()
        self.assertIsNone(self.ui.delete_phase)
        self.assertNotIn('disabled', window.start_button.state())
        self.assertNotIn('disabled', self.ui.gallery_box.state())
        self.assertNotIn('disabled', self.ui.query_button.state())
        self.start(window)
        self.assertEqual(self.ui.delete_phase, 'defense')
        self.assertIn('disabled', self.ui.gallery_box.state())
        self.assertIn('disabled', self.ui.query_button.state())
        self.ui.query()
        self.ui.prepare_delete()
        self.ui.show_defense()
        self.ui.reader.assert_not_called()
        self.assertIs(self.ui.defense_window, window)
        window.start()
        self.assertEqual(window.task.starts, 1)
        self.assertFalse(window.task.include_new_members)
        self.assertEqual(window.task.gallery, 'mnt')
        self.tick(window)
        self.assertIn('요청 수락 누적 2건', window.counts.get())
        self.assertIn('응답 불명 0회', window.counts.get())
        self.assertIn('수락 누적 2건', window.metric_totals.get())

    def test_stop_and_close_wait_for_worker_then_unlock_manual(self):
        window = self.window()
        self.start(window)
        window.close()
        self.assertTrue(window.task.cancel.is_set())
        self.assertFalse(window.closed)
        self.assertEqual(self.ui.delete_phase, 'defense')
        window.task.finish()
        self.tick(window)
        self.assertTrue(window.closed)
        self.assertIsNone(self.ui.delete_phase)
        self.assertFalse(self.ui.closed)
        self.assertNotIn('disabled', self.ui.query_button.state())

    def test_main_window_close_requests_stop_and_waits_for_finished_worker(self):
        window = self.window()
        self.start(window)
        self.ui.close()
        self.assertFalse(self.ui.closed)
        self.assertTrue(window.task.cancel.is_set())
        window.task.finish()
        self.tick(window)
        self.assertTrue(self.ui.closed)

    def test_preparing_or_executing_manual_work_cannot_open_defense(self):
        for phase in ('permission', 'ready', 'executing'):
            self.ui._delete_state(phase)
            self.ui.show_defense()
            self.assertIsNone(self.ui.defense_window)
        self.ui._delete_state(None)

    def test_captcha_prompt_on_ui_thread_failure_still_releases_worker(self):
        window = self.window()
        self.start(window)
        request = CaptchaRequest(GIF)
        window.task.captchas.put(request)
        with patch('rectifier.defense_ui.prompt_captcha', side_effect=RuntimeError('private sentinel')):
            self.tick(window)
        self.assertTrue(request.ready.is_set())
        self.assertEqual(request.image, b'')
        self.assertNotIn('private', window.status.get())

    def test_owned_captcha_dialog_closes_on_cancel_without_closing_main(self):
        cancel = Event()
        self.root.after(120, cancel.set)
        answer = prompt_captcha(GIF, parent=self.root, cancelled=cancel.is_set)
        self.assertIsNone(answer)
        self.assertTrue(self.root.winfo_exists())

    def test_activity_line_formats_elapsed_and_last_accept(self):
        from rectifier.defense_ui import activity_line
        self.assertEqual(activity_line(65, None), '경과 1:05 · 마지막 삭제 수락 아직 없음')
        self.assertEqual(activity_line(3725, 9), '경과 1:02:05 · 마지막 삭제 수락 0:09 전')

    def test_running_window_shows_elapsed_last_accept_and_idle_message(self):
        window = self.window()
        self.start(window)
        self.tick(window)
        self.assertIn('경과 ', window.counts.get())
        self.assertIn('마지막 삭제 수락 0:00 전', window.counts.get())  # FakeTask already reports 2 accepted.
        self.assertIn('새 글 없음 · 정상 감시 중', window.status.get())
        window.task.state = TaskState('running', DefenseRunStats(2, 1, 2, 0, 0, None, 13, last_candidates=3))
        self.tick(window)
        self.assertNotIn('새 글 없음', window.status.get())
