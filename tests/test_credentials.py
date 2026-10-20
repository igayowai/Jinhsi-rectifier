import unittest
from unittest.mock import Mock, patch

from rectifier.credentials import CredentialError, _backend, read_password, save_password_new, forget_password


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.backend = Mock()
        self.patcher = patch("rectifier.credentials._backend", return_value=self.backend)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_exact_namespace_and_account(self):
        self.backend.get_password.return_value = "synthetic-secret"
        self.assertEqual(read_password("Synthetic_Account"), "synthetic-secret")
        self.backend.get_password.assert_called_once_with("rectifier:dcinside:synthetic_account", "synthetic_account")

    def test_no_username_ambiguity(self):
        for account in [None, "", "a/b", "a@b", "a\nb"]:
            with self.subTest(account=account), self.assertRaises(CredentialError):
                read_password(account)
        self.backend.get_password.assert_not_called()

    def test_no_overwrite(self):
        self.backend.get_password.return_value = "synthetic-existing"
        with self.assertRaises(CredentialError):
            save_password_new("test", "synthetic-new")
        self.backend.set_password.assert_not_called()

    def test_create_and_verify(self):
        self.backend.get_password.side_effect = [None, "synthetic-value"]
        save_password_new("test", "synthetic-value")
        self.backend.set_password.assert_called_once_with("rectifier:dcinside:test", "test", "synthetic-value")

    def test_delete_only_own_entry_and_verify(self):
        self.backend.get_password.side_effect = ["synthetic-value", None]
        self.assertTrue(forget_password("test"))
        self.backend.delete_password.assert_called_once_with("rectifier:dcinside:test", "test")

    def test_missing_entry_never_deleted(self):
        self.backend.get_password.return_value = None
        self.assertFalse(forget_password("test"))
        self.backend.delete_password.assert_not_called()

    def test_backend_errors_do_not_escape_as_secret_text(self):
        self.backend.get_password.side_effect = RuntimeError("synthetic-secret")
        for operation in [lambda: read_password("test"), lambda: save_password_new("test", "x"), lambda: forget_password("test")]:
            with self.subTest(operation=operation):
                try:
                    operation()
                except CredentialError as error:
                    self.assertNotIn("synthetic-secret", str(error))
                else:
                    self.fail("expected fixed diagnostic")

    def test_invalid_password_never_written(self):
        for password in [None, "", "x\ny", "x\0y", "x" * 1281]:
            with self.subTest(password_type=type(password).__name__), self.assertRaises(CredentialError):
                save_password_new("test", password)
        self.backend.set_password.assert_not_called()


@unittest.skipUnless(__import__("sys").platform == "win32", "Windows backend")
class BackendTests(unittest.TestCase):
    def test_backend_is_windows_local_even_with_other_default(self):
        with patch("keyring.backends.Windows.WinVaultKeyring") as constructor:
            instance = _backend()
            self.assertIs(instance, constructor.return_value)
            self.assertEqual(instance.persist, "local machine")
