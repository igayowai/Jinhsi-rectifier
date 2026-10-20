from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import tkinter as tk
from time import monotonic, sleep
from unittest import TestCase
from unittest.mock import Mock, patch

from rectifier.account_profiles import AccountProfiles, ProfileError, Profiles
from rectifier.account_task import LoginTask
from rectifier.auth import AuthStatus, LoginResult
from rectifier.credentials import save_verified_password
from rectifier.manager_ui import ManagerUI


class ProfileStoreTests(TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / '계정 목록/accounts.sqlite'
        self.writer = Mock()
        self.store = AccountProfiles(self.path, password_writer=self.writer)

    def test_first_read_and_logout_create_nothing(self):
        self.assertEqual(self.store.load(), Profiles())
        self.store.disable_auto()
        self.assertFalse(self.path.parent.exists())

    def test_multiple_accounts_reload_last_and_no_secret_in_database(self):
        self.store.remember('Alpha', 'synthetic-secret-A', auto_login=True)
        self.store.remember('beta', 'synthetic-secret-B', auto_login=True)
        loaded = AccountProfiles(self.path).load()
        self.assertEqual(loaded, Profiles(('alpha', 'beta'), 'beta', True))
        data = self.path.read_bytes()
        self.assertNotIn(b'synthetic-secret', data)
        self.writer.assert_any_call('alpha', 'synthetic-secret-A')
        self.store.disable_auto()
        self.assertEqual(self.store.load(), Profiles(('alpha', 'beta'), 'beta', False))

    def test_existing_saved_account_does_not_rewrite_password(self):
        self.store.remember('alpha', 'synthetic', auto_login=True, write_password=False)
        self.writer.assert_not_called()
        self.assertEqual(self.store.load().accounts, ('alpha',))

    def test_writer_failure_preserves_previous_preferences(self):
        self.store.remember('alpha', 'synthetic', auto_login=True)
        previous = self.path.read_bytes()
        self.writer.side_effect = RuntimeError('sensitive backend output')
        with self.assertRaises(ProfileError) as error:
            self.store.remember('beta', 'synthetic', auto_login=True)
        self.assertNotIn('sensitive', str(error.exception))
        self.assertEqual(self.path.read_bytes(), previous)

    def test_corrupt_registry_preserved_without_vault_access(self):
        self.path.parent.mkdir()
        self.path.write_bytes(b'synthetic-broken-database')
        with self.assertRaises(ProfileError):
            self.store.remember('alpha', 'synthetic', auto_login=True)
        self.writer.assert_not_called()
        self.assertEqual(self.path.read_bytes(), b'synthetic-broken-database')

    def test_logout_of_old_account_can_preserve_new_account_preference(self):
        self.store.remember('beta', 'synthetic', auto_login=True)
        self.store.disable_auto('alpha')
        self.assertTrue(self.store.load().auto_login)
        self.store.disable_auto('beta')
        self.assertFalse(self.store.load().auto_login)

    def test_successful_password_update_uses_exact_vault_target(self):
        backend = Mock()
        backend.get_password.return_value = 'synthetic-new'
        with patch('rectifier.credentials._backend', return_value=backend):
            save_verified_password('Alpha', 'synthetic-new')
        backend.set_password.assert_called_once_with('rectifier:dcinside:alpha', 'alpha', 'synthetic-new')


class VerifiedSaveTests(TestCase):
    def result(self, account='alpha', status=AuthStatus.VERIFIED):
        return LoginResult(status, 'synthetic', Mock(), account)

    def test_store_only_verified_matching_account(self):
        for result in (self.result('other'), self.result(status=AuthStatus.REJECTED)):
            save = Mock()
            task = LoginTask('alpha', 'synthetic', login_fn=Mock(return_value=result), on_verified=save)
            task._work()
            save.assert_not_called()
            self.assertIsNone(task._password)
            self.assertIsNone(task._on_verified)
            result.session.close.assert_called_once()

    def test_cancel_during_login_does_not_save(self):
        save = Mock()
        task = LoginTask('alpha', 'synthetic', on_verified=save)
        def login(*_, **__):
            task.stop()
            return self.result()
        task._login = login
        task._work()
        save.assert_not_called()
        self.assertIsNone(task.result)

    def test_save_failure_keeps_verified_session_without_exposing_error(self):
        result = self.result()
        task = LoginTask('alpha', 'synthetic', login_fn=Mock(return_value=result),
                         on_verified=Mock(side_effect=RuntimeError('sensitive-secret')))
        task._work()
        self.assertIs(task.take_result(), result)
        self.assertFalse(task.profile_saved)
        self.assertNotIn('sensitive-secret', task.profile_warning)
        self.assertIsNone(task._password)
        self.assertIsNone(task._on_verified)


class ProfileUITests(TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'accounts.sqlite'
        self.vault = {}
        self.enterContext(patch('rectifier.defense_monitor.write_summary', return_value=True))
        self.store = AccountProfiles(self.path, password_writer=lambda account, password: self.vault.update({account: password}))
        self.calls = []
        self.sessions = []
        def login(account, password, **_):
            self.calls.append((account, password))
            session = Mock()
            self.sessions.append(session)
            return LoginResult(AuthStatus.VERIFIED, 'synthetic', session, account)
        self.login_fn = Mock(side_effect=login)
        self.factory = lambda account, password, **kwargs: LoginTask(account, password,
            password_reader=self.vault.get, login_fn=self.login_fn, **kwargs)
        self.network = patch('socket.socket', side_effect=AssertionError('offline only'))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.ui = None

    def open(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.ui = ManagerUI(self.root, profile_store=self.store, login_task_factory=self.factory)
        self.ui.check_permission = Mock()

    def wait(self, condition):
        deadline = monotonic() + 3
        while not condition() and monotonic() < deadline:
            self.root.update()
            sleep(.01)
        self.assertTrue(condition())

    def tearDown(self):
        if self.ui is not None and not self.ui.closed:
            self.ui.close()
            self.wait(lambda: self.ui.closed)

    def login(self, account):
        self.ui.account_text.set(account)
        self.ui.password_text.set('synthetic-' + account)
        self.ui.use_saved.set(False)
        self.ui.sign_in()
        self.wait(lambda: self.ui.login_task is None)

    def test_first_login_saves_reopen_auto_once_and_logout_disables(self):
        self.open()
        self.login('alpha')
        self.assertEqual(self.store.load(), Profiles(('alpha',), 'alpha', True))
        self.ui.check_permission.assert_called_once()
        self.ui.close()
        self.open()
        self.wait(lambda: self.ui.login_result is not None)
        self.assertEqual([account for account, _ in self.calls], ['alpha', 'alpha'])
        self.ui.sign_out()
        self.assertFalse(self.store.load().auto_login)
        self.ui.close()
        self.open()
        self.root.update()
        self.assertIsNone(self.ui.auto_login_id)
        self.assertIsNone(self.ui.login_task)
        self.assertEqual(self.ui.account_text.get(), 'alpha')

    def test_multiple_accounts_switch_clears_permission_and_session(self):
        self.open()
        self.login('alpha')
        self.ui.new_account()
        self.sessions[0].close.assert_called_once()
        self.assertEqual(self.ui.account_text.get(), '')
        self.assertIn('미확인', self.ui.permission_status.get())
        self.login('beta')
        self.assertEqual(self.store.load().accounts, ('alpha', 'beta'))
        self.ui.sign_out()
        self.ui.account_text.set('alpha')
        self.assertTrue(self.ui.use_saved.get())
        self.ui.sign_in()
        self.wait(lambda: self.ui.login_task is None)
        self.assertEqual(self.ui.login_result.verified_user_id, 'alpha')
        self.assertEqual(self.calls[-1], ('alpha', 'synthetic-alpha'))

    def test_auto_failure_is_single_attempt_and_allows_manual_login(self):
        self.store.remember('alpha', 'synthetic', auto_login=True)
        self.login_fn.side_effect = None
        self.login_fn.return_value = LoginResult(AuthStatus.REJECTED, 'synthetic')
        self.open()
        self.wait(lambda: self.ui.auto_login_id is None and self.ui.login_task is None)
        self.assertEqual(self.login_fn.call_count, 1)
        self.assertFalse(self.store.load().auto_login)
        self.assertEqual(str(self.ui.login_button.cget('state')), 'normal')

    def test_closing_before_scheduled_login_does_not_access_password(self):
        self.store.remember('alpha', 'synthetic', auto_login=True)
        self.open()
        self.ui.close()
        self.login_fn.assert_not_called()

    def test_opt_out_does_not_save_new_account(self):
        self.open()
        self.ui.remember_account.set(False)
        self.login('alpha')
        self.assertFalse(self.path.exists())
        self.assertEqual(self.vault, {})
        self.assertIsNotNone(self.ui.login_result)

    def test_typing_other_account_cancels_scheduled_auto_login(self):
        self.store.remember('alpha', 'synthetic', auto_login=True)
        self.open()
        self.ui.account_text.set('beta')
        self.assertIsNone(self.ui.auto_login_id)
        self.assertFalse(self.ui.use_saved.get())
        self.login_fn.assert_not_called()

    def test_selected_saved_account_is_borrowed_by_defense(self):
        from queue import Queue
        from threading import Event
        from rectifier.defense_task import TaskState
        self.store.remember('alpha', 'synthetic-A', auto_login=False)
        self.store.remember('beta', 'synthetic-B', auto_login=False)
        self.open()
        self.ui.account_text.set('alpha')
        self.ui.sign_in()
        self.wait(lambda: self.ui.login_task is None)
        current = self.ui.login_result
        self.ui.gallery.set('mnt')
        with patch('rectifier.manager_ui.DefenseTask') as constructor:
            task = constructor.return_value
            task.alive, task.state = False, TaskState()
            task.captchas, task.cancel = Queue(), Event()
            self.ui.show_defense()
            window = self.ui.defense_window
            window.start()
            constructor.assert_called_once_with('mnt', (1,), login_result=current, include_new_members=False)
            self.ui.new_account()
            self.assertIs(self.ui.login_result, current)
            window.close()
        current.session.close.assert_not_called()
