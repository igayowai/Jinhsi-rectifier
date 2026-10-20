from threading import Event
from unittest import TestCase
from unittest.mock import Mock, patch

from rectifier.auth import AuthStatus, LoginResult
from rectifier.defense_runner import ListDefenseAdapter, DefenseRunner
from rectifier.management import check_management
from rectifier.manager_ui import ManagerUI
from queue import Queue
from tests.common import MENU, response


class EmptyManagementTests(TestCase):
    def setUp(self):
        block = patch('socket.socket', side_effect=AssertionError('offline only'))
        block.start()
        self.addCleanup(block.stop)
        self.session = Mock()
        self.login = LoginResult(AuthStatus.VERIFIED, 'synthetic', self.session, 'syntheticadmin')

    def test_empty_or_whitespace_200_list_is_not_permission_denial(self):
        for body in ('', ' \r\n\t'):
            with self.subTest(body=repr(body)):
                self.session.reset_mock()
                self.session.request.side_effect = [response(MENU), response(body)]
                runner = DefenseRunner(ListDefenseAdapter(self.login, 'mnt'), after_post_id=1)
                self.assertEqual(runner.step().stopped, 'empty_response')
                self.assertEqual(runner.stats.requests, 0)
                self.assertEqual(self.session.request.call_count, 2)
                runner.step()
                self.assertEqual(self.session.request.call_count, 2)  # No blind retry.

    def test_empty_identity_response_is_read_failure_too(self):
        self.session.request.return_value = response('')
        runner = DefenseRunner(ListDefenseAdapter(self.login, 'mnt'), after_post_id=1)
        self.assertEqual(runner.step().stopped, 'empty_response')
        self.assertEqual(self.session.request.call_count, 1)

    def test_cli_check_reports_empty_response_and_status_200(self):
        self.session.request.return_value = response('')
        report = check_management(self.login, 'mnt')
        self.assertEqual(report['status'], 'empty_response')
        self.assertEqual(report['http_status'], 200)
        self.assertFalse(report['delete_control_observed'])

    def test_real_403_stays_forbidden_even_if_body_empty(self):
        self.session.request.side_effect = [response(MENU), response('', 403)]
        runner = DefenseRunner(ListDefenseAdapter(self.login, 'mnt'), after_post_id=1)
        self.assertEqual(runner.step().stopped, 'forbidden')

    def test_ui_does_not_report_missing_permission_for_empty_page(self):
        ui = Mock()
        ui.delete_messages = Queue()
        ui.delete_messages.put(('permission', ('mnt', 'empty_response')))
        ManagerUI._poll_delete(ui)
        message = ui.permission_status.set.call_args.args[0]
        self.assertIn('목록 조회 실패', message)
        self.assertIn('권한을 판단할 수 없어', message)
        self.assertNotIn('관리 삭제 권한을 확인하지 못함', message)
