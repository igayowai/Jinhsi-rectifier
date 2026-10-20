from unittest import TestCase
from unittest.mock import Mock, patch
from urllib.error import URLError

from rectifier.account_task import LoginTask, _failure_message
from rectifier.auth import AuthStatus, AuthStep, LoginResult, MemorySession, login
from tests.test_auth import FakeOpener, FakeResponse, FORM, PASSWORD


class LoginDiagnosticTests(TestCase):
    def run_login(self, responses):
        opener = FakeOpener(responses)
        session = MemorySession(opener=opener)
        def attempt(account, password, **kwargs):
            return login(account, password, session=session, **kwargs)
        task = LoginTask('synthetic-account', PASSWORD, login_fn=attempt)
        with patch('socket.socket', side_effect=AssertionError('offline only')):
            task._work()
        self.assertIsNone(task.result)
        self.assertIsNone(task._password)
        self.assertTrue(session._closed)
        self.assertNotIn(PASSWORD, task.failure_message)
        return task, opener

    def test_server_error_reports_stage_without_retry(self):
        task, opener = self.run_login([FakeResponse(body=FORM), FakeResponse(500)])
        self.assertEqual(task.failure_message, '디시 인증 서버 오류 응답 [사전 확인 · HTTP 500]')
        self.assertEqual(len(opener.requests), 2)

    def test_security_character_rejection_is_not_password_error(self):
        task, _ = self.run_login([FakeResponse(body=FORM),
                                 FakeResponse(body='{"reason":"wrong_code"}')])
        self.assertIn('보안문자가 거절됨', task.failure_message)

    def test_network_error_is_not_http_rejection(self):
        task, _ = self.run_login([URLError('PRIVATE_DIAGNOSTIC')])
        self.assertIn('인증 통신', task.failure_message)
        self.assertNotIn('HTTP', task.failure_message)
        self.assertNotIn('PRIVATE_DIAGNOSTIC', task.failure_message)

    def test_untrusted_message_and_stage_are_not_displayed(self):
        result = LoginResult(AuthStatus.REJECTED, PASSWORD,
                             diagnostics=(AuthStep(PASSWORD, PASSWORD),))
        message = _failure_message(result)
        self.assertNotIn(PASSWORD, message)
        self.assertIn('인증 처리', message)

    def test_cancel_keeps_cancel_reason(self):
        task = LoginTask('synthetic-account', PASSWORD)
        def attempt(*args, **kwargs):
            task.stop()
            return LoginResult(AuthStatus.REJECTED, 'synthetic')
        task._login = attempt
        task._work()
        self.assertEqual(task.state.reason, 'user_stopped')
        self.assertIsNone(task.failure_message)

