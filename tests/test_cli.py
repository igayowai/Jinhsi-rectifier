from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from getpass import GetPassWarning
from types import SimpleNamespace
import warnings
from unittest import TestCase
from unittest.mock import Mock, patch

from rectifier.__main__ import main, prompt_captcha
from rectifier.auth import AuthStatus, LoginResult


class CliTests(TestCase):
    def setUp(self):
        # Unit tests must never read the real user's credential store.
        self.password_reader = patch("rectifier.__main__.read_password", return_value=None)
        self.read_password = self.password_reader.start()
        self.addCleanup(self.password_reader.stop)

    def test_demo_is_offline_and_excludes_member_and_unknown(self):
        with patch("socket.socket", side_effect=AssertionError("network forbidden")):
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["demo"]), 0)
        data = json.loads(output.getvalue())
        self.assertEqual(data["live_requests"], 0)
        self.assertEqual(data["target_count"], 1)
        self.assertEqual([item["disposition"] for item in data["plan"]["items"]],
                         ["delete_guest", "protected_member", "unknown_author"])

    def test_invalid_scope_is_checked_before_network(self):
        with patch("rectifier.__main__.read_public_board") as read, redirect_stderr(StringIO()):
            code = main(["preview", "--gallery", "mnt", "--account", "x", "--ids", "1", "2", "3", "4", "--page", "1"])
            self.assertEqual(code, 2)
            read.assert_not_called()

    def test_local_preview_has_no_authentication_or_live_action(self):
        output = StringIO()
        with patch("socket.socket", side_effect=AssertionError("network forbidden")), redirect_stdout(output):
            code = main(["preview", "--gallery", "fixture", "--account", "synthetic_account", "--ids", "106", "105", "103",
                         "--html", "tests/fixtures/mixed_board.html"])
        self.assertEqual(code, 0)
        data = json.loads(output.getvalue())
        self.assertEqual(data["target_count"], 1)
        self.assertFalse(data["authenticated"])
        self.assertFalse(data["live_delete_available"])

    def test_login_check_requires_interactive_hidden_input(self):
        with patch("rectifier.__main__.sys.stdin", Mock(isatty=lambda: False)), \
                patch("rectifier.auth.login") as login, redirect_stderr(StringIO()):
            self.assertEqual(main(["login-check", "--account", "synthetic_account"]), 2)
            login.assert_not_called()

    def test_login_check_discards_session_without_printing_password(self):
        session = Mock()
        result = LoginResult(AuthStatus.ACCEPTED_UNVERIFIED, "synthetic safe status", session)
        output = StringIO()
        with patch("rectifier.__main__.sys.stdin", Mock(isatty=lambda: True)), \
                patch("rectifier.__main__.getpass", return_value="synthetic-secret"), \
                patch("rectifier.auth.login", return_value=result) as login, redirect_stdout(output):
            self.assertEqual(main(["login-check", "--account", "synthetic_account"]), 0)
            login.assert_called_once_with("synthetic_account", "synthetic-secret", captcha_prompt=prompt_captcha)
        session.close.assert_called_once()
        self.assertNotIn("synthetic-secret", output.getvalue())
        self.assertFalse(json.loads(output.getvalue())["identity_verified"])

    def test_getpass_warning_cannot_fall_back_to_echo(self):
        def no_echo():
            warnings.warn("synthetic warning", GetPassWarning)
            raise AssertionError("would echo password")
        with patch("rectifier.__main__.sys.stdin", Mock(isatty=lambda: True)), \
                patch("rectifier.__main__.getpass", side_effect=lambda *args: no_echo()), \
                patch("rectifier.auth.login") as login, redirect_stderr(StringIO()):
            self.assertEqual(main(["login-check", "--account", "synthetic_account"]), 2)
            login.assert_not_called()

    def test_login_exceptions_and_bad_arguments_do_not_echo_secrets(self):
        output = StringIO()
        with patch("rectifier.__main__.sys.stdin", Mock(isatty=lambda: True)), \
                patch("rectifier.__main__.getpass", return_value="synthetic-secret"), \
                patch("rectifier.auth.login", side_effect=RuntimeError("synthetic-secret")), redirect_stderr(output):
            self.assertEqual(main(["login-check", "--account", "synthetic_account"]), 2)
        self.assertNotIn("synthetic-secret", output.getvalue())

    def test_saved_password_works_without_terminal_or_manual_input(self):
        self.read_password.return_value = "synthetic-stored-value"
        session = Mock()
        result = LoginResult(AuthStatus.ACCEPTED_UNVERIFIED, "safe status", session)
        output = StringIO()
        with patch("rectifier.__main__.sys.stdin", Mock(isatty=lambda: False)), \
                patch("rectifier.__main__.getpass") as prompt, \
                patch("rectifier.auth.login", return_value=result) as login, redirect_stdout(output):
            self.assertEqual(main(["login-check", "--account", "synthetic_account"]), 0)
            prompt.assert_not_called()
            login.assert_called_once_with("synthetic_account", "synthetic-stored-value", captcha_prompt=prompt_captcha)
        self.assertNotIn("synthetic-stored-value", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["password_source"], "windows_credential_manager")
        session.close.assert_called_once()

    def test_manual_mode_bypasses_store(self):
        with patch("rectifier.__main__.sys.stdin", Mock(isatty=lambda: True)), \
                patch("rectifier.__main__.getpass", return_value="synthetic-value"), \
                patch("rectifier.auth.login", return_value=LoginResult(AuthStatus.REJECTED, "safe")), redirect_stdout(StringIO()):
            self.assertEqual(main(["login-check", "--account", "synthetic_account", "--manual"]), 2)
        self.read_password.assert_not_called()

    def test_verified_account_is_reported_without_claiming_delete_authority(self):
        self.read_password.return_value = "synthetic-stored-value"
        session = Mock()
        result = LoginResult(AuthStatus.VERIFIED, "safe status", session, "synthetic_account")
        output = StringIO()
        with patch("rectifier.auth.login", return_value=result), redirect_stdout(output):
            self.assertEqual(main(["login-check", "--account", "synthetic_account"]), 0)
        data = json.loads(output.getvalue())
        self.assertEqual(data["status"], "verified")
        self.assertTrue(data["identity_verified"])
        self.assertEqual(data["verified_user_id"], "synthetic_account")
        self.assertFalse(data["live_delete_available"])
        session.close.assert_called_once()

    def test_save_and_forget_use_only_requested_account(self):
        with patch("rectifier.__main__.sys.stdin", Mock(isatty=lambda: True)), \
                patch("rectifier.__main__.getpass", return_value="synthetic-value"), \
                patch("rectifier.__main__.save_password_new") as save, redirect_stdout(StringIO()):
            self.assertEqual(main(["password-save", "--account", "synthetic_account"]), 0)
            save.assert_called_once_with("synthetic_account", "synthetic-value")
        with patch("rectifier.__main__.forget_password", return_value=True) as forget, redirect_stdout(StringIO()):
            self.assertEqual(main(["password-forget", "--account", "synthetic_account"]), 0)
            forget.assert_called_once_with("synthetic_account")

    def test_bad_arguments_do_not_echo_secret(self):
        output = StringIO()
        with redirect_stderr(output), self.assertRaises(SystemExit):
            main(["login-check", "--account", "synthetic_account", "--password", "synthetic-secret"])
        self.assertNotIn("synthetic-secret", output.getvalue())

    def test_gallery_validation_precedes_credentials_and_login(self):
        with patch("rectifier.auth.login") as login, redirect_stderr(StringIO()):
            self.assertEqual(main(["login-check", "--account", "synthetic", "--gallery", "../x"]), 2)
        self.read_password.assert_not_called()
        login.assert_not_called()

    def test_management_error_is_reported_and_session_closed(self):
        self.read_password.return_value = "synthetic-stored-value"
        session = Mock()
        result = LoginResult(AuthStatus.VERIFIED, "safe", session, "synthetic")
        from rectifier.auth import HttpResponse
        session.request.return_value = HttpResponse(500, {}, "synthetic-private-payload")
        output = StringIO()
        with patch("rectifier.auth.login", return_value=result), redirect_stdout(output):
            self.assertEqual(main(["login-check", "--account", "synthetic", "--gallery", "mnt"]), 2)
        data = json.loads(output.getvalue())
        self.assertEqual(data["management"]["http_status"], 500)
        self.assertFalse(data["live_delete_available"])
        self.assertNotIn("synthetic-private-payload", output.getvalue())
        self.assertNotIn("synthetic-stored-value", output.getvalue())
        session.close.assert_called_once()
