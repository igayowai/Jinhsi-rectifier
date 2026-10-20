from dataclasses import replace
from threading import Event
from unittest import TestCase
from unittest.mock import Mock, patch

from rectifier.account_task import LoginTask
from rectifier.auth import AuthStatus, LoginResult, DEFAULT_RETURN_URL
from rectifier.batch_journal import BatchResponse
from rectifier.defense_runner import DefenseReadError, DefenseRunner, ListDefenseAdapter
from rectifier.defense_task import DefenseTask
from rectifier.manual_action import ManualAction
from rectifier.policy import DeletePolicy
from rectifier.selection import SelectionReview, SelectionRow
from tests.common import listing
from tests.common import CONTROL, MENU, response


ACCOUNT = 'gallerymanager'


def review(ids=(101,), disposition='delete_guest'):
    return SelectionReview('mnt', (1,), tuple(SelectionRow(
        pid, 'synthetic old post', disposition, 'ordinary') for pid in ids), True)


class AccountManualTests(TestCase):
    def setUp(self):
        for target in ('socket.socket', 'sqlite3.connect'):
            blocked = patch(target, side_effect=AssertionError('offline / no journal'))
            blocked.start()
            self.addCleanup(blocked.stop)
        self.cancel = Event()
        self.menu = MENU.replace('syntheticadmin', ACCOUNT)
        self.page = listing((101, 102, 103)) + CONTROL
        self.outcome = response('{"result":"success"}')
        self.session = Mock()
        self.session.cookie_value.return_value = 'synthetic-token'
        self.session.request.side_effect = self.request
        self.login = LoginResult(AuthStatus.VERIFIED, 'synthetic', self.session, ACCOUNT)

    def request(self, method, url, **kwargs):
        if method == 'POST':
            if isinstance(self.outcome, Exception):
                raise self.outcome
            return self.outcome
        if url == DEFAULT_RETURN_URL:
            return response(self.menu)
        self.assertIn('/board/lists/', url)
        return response(self.page)

    def posts(self):
        return [c for c in self.session.request.call_args_list if c.args[0] == 'POST']

    def test_other_account_selected_ids_only_no_archive_or_db(self):
        action = ManualAction(self.login, review((101, 103)))
        receipt = action.execute(self.cancel)
        self.assertEqual(receipt.response, BatchResponse.SUCCESS_REPORTED)
        self.assertEqual([v for k, v in self.posts()[0].kwargs['form'] if k == 'nos[]'], ['101', '103'])
        self.assertEqual(len(self.session.request.call_args_list), 3)
        self.session.close.assert_not_called()
        with self.assertRaisesRegex(DefenseReadError, 'already_used'):
            action.execute(self.cancel)
        self.assertEqual(len(self.posts()), 1)

    def test_manual_members_allowed_defense_still_guest_only(self):
        self.page = listing((101,), writer='data-uid="testmember" data-ip=""') + CONTROL
        manual = ManualAction(self.login, review(disposition='protected_member'))
        self.assertTrue(manual.execute(self.cancel).attempted)
        self.assertFalse(ListDefenseAdapter(self.login, 'mnt').read_snapshot(self.cancel).candidates)
        with self.assertRaises(ValueError):
            DefenseRunner(manual.adapter, after_post_id=1)

    def test_changed_identity_blocks_post(self):
        self.menu = MENU
        with self.assertRaisesRegex(DefenseReadError, 'auth_required'):
            ManualAction(self.login, review()).execute(self.cancel)
        self.assertFalse(self.posts())

    def test_missing_controls_blocks_post(self):
        self.page = listing((101,))
        with self.assertRaisesRegex(DefenseReadError, 'forbidden'):
            ManualAction(self.login, review()).execute(self.cancel)
        self.assertFalse(self.posts())

    def test_changed_title_author_kind_or_missing_target_block_post(self):
        for page in (listing((102,)), listing((101,)).replace('synthetic old post', 'changed'),
                     listing((101,), writer='data-uid="member" data-ip=""'),
                     listing((101,), kind='icon_recomimg')):
            self.page = page + CONTROL
            with self.subTest(page=page), self.assertRaises(DefenseReadError):
                ManualAction(self.login, review()).execute(self.cancel)
        self.assertFalse(self.posts())

    def test_cross_page_notice_blocks_selected_id(self):
        from tests.test_parser import board, post_row
        self.page = board(post_row(number='공지', attrs='data-no="101"')) + listing((101,)) + CONTROL
        with self.assertRaises(DefenseReadError):
            ManualAction(self.login, review()).execute(self.cancel)
        self.assertFalse(self.posts())

    def test_cancel_before_start_skips_network(self):
        self.cancel.set()
        with self.assertRaisesRegex(DefenseReadError, 'user_stopped'):
            ManualAction(self.login, review()).execute(self.cancel)
        self.session.request.assert_not_called()

    def test_unknown_response_no_retry(self):
        self.outcome = TimeoutError()
        action = ManualAction(self.login, review())
        receipt = action.execute(self.cancel)
        self.assertTrue(receipt.attempted)
        self.assertEqual(receipt.response, BatchResponse.UNKNOWN)
        with self.assertRaises(DefenseReadError):
            action.execute(self.cancel)
        self.assertEqual(len(self.posts()), 1)

    def test_quota_or_auth_rejection_not_success(self):
        self.outcome = response('denied', 429)
        receipt = ManualAction(self.login, review()).execute(self.cancel)
        self.assertEqual(receipt.response, BatchResponse.REJECTION_REPORTED)
        self.assertEqual(receipt.stopped, 'rate_limited')

    def test_login_transfers_session_once_and_clears_password(self):
        login_fn = Mock(return_value=self.login)
        reader = Mock(side_effect=AssertionError('vault forbidden'))
        task = LoginTask('GalleryManager', 'synthetic-password', login_fn=login_fn, password_reader=reader)
        task.start()
        task._thread.join(2)
        self.assertFalse(task.alive)
        self.assertIs(task.take_result(), self.login)
        self.assertIsNone(task.take_result())
        self.assertIsNone(task._password)
        self.assertEqual(login_fn.call_args.args[0], ACCOUNT)
        self.assertNotIn('synthetic-password', repr(task.state))
        reader.assert_not_called()
        self.session.close.assert_not_called()

    def test_saved_password_is_explicit_and_uses_entered_account(self):
        reader, login_fn = Mock(return_value='synthetic-saved'), Mock(return_value=self.login)
        task = LoginTask(ACCOUNT, '', use_saved=True, password_reader=reader, login_fn=login_fn)
        task._work()
        reader.assert_called_once_with(ACCOUNT)
        self.assertEqual(login_fn.call_args.args, (ACCOUNT, 'synthetic-saved'))

    def test_login_identity_mismatch_closes_session(self):
        task = LoginTask('different', 'synthetic', login_fn=Mock(return_value=self.login))
        task._work()
        self.assertIsNone(task.result)
        self.session.close.assert_called_once()
        self.assertEqual(task.state.reason, 'auth_required')

    def test_cancel_while_reading_saved_password_never_logs_in(self):
        login_fn = Mock()
        task = LoginTask(ACCOUNT, '', use_saved=True, login_fn=login_fn)
        def read(_):
            task.stop()
            return 'synthetic-saved'
        task._password_reader = read
        task._work()
        login_fn.assert_not_called()
        self.assertIsNone(task._password)

    def test_defense_requires_explicit_account_or_verified_session(self):
        with self.assertRaises(ValueError):
            # CredentialError is a RuntimeError, so check the exact type below.
            ListDefenseAdapter(replace(self.login, verified_user_id=None), 'mnt')
        from rectifier.credentials import CredentialError
        with self.assertRaises(CredentialError):
            DefenseTask('mnt', (1,))

    def test_login_cancelled_after_response_closes_session(self):
        task = LoginTask(ACCOUNT, 'synthetic')
        def login(*args, **kwargs):
            task.stop()
            return self.login
        task._login = login
        task._work()
        self.assertIsNone(task.result)
        self.session.close.assert_called_once()

    def test_borrowed_defense_neither_relogs_nor_closes_app_session(self):
        reader, login_fn = Mock(), Mock()
        task = DefenseTask('mnt', (1,), login_result=self.login,
                           password_reader=reader, login_fn=login_fn)
        runner = Mock()
        runner.run.return_value.stopped = 'user_stopped'
        with patch('rectifier.defense_task.DefenseRunner', return_value=runner):
            task._work()
        reader.assert_not_called()
        login_fn.assert_not_called()
        self.session.close.assert_not_called()
        self.assertEqual(task.account, ACCOUNT)
