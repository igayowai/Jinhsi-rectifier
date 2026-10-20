from unittest import TestCase
from unittest.mock import Mock, patch

from rectifier.auth import AuthStatus, HttpResponse, LoginResult
from rectifier.demo import DEMO_HTML
from rectifier.management import check_management


CONTROL = '<div class="list_bottom_btnbox"><div class="useradmin_btnbox"><button onclick="del_list()">삭제</button></div></div>'


class ManagementTests(TestCase):
    def test_official_static_template_is_recognized_without_running_js(self):
        template = '<script id="minor_buttons-tmpl" type="text/x-jquery-tmpl">' + CONTROL + '</script>'
        result = self.inspect(DEMO_HTML + template)
        self.assertEqual(result['status'], 'control_observed')
        self.assertEqual(result['delete_control_source'], 'server_template')

    def test_ambiguous_dynamic_unclosed_or_wrong_template_is_not_evidence(self):
        template = '<script id="minor_buttons-tmpl" type="text/x-jquery-tmpl">' + CONTROL + '</script>'
        for invalid in (template + template, template + CONTROL,
                        template.replace('text/x-jquery-tmpl', 'text/javascript'),
                        template.replace('minor_buttons-tmpl', 'untrusted-template'),
                        template.replace('</script>', ''),
                        template.replace(CONTROL, '{{if granted}}' + CONTROL + '{{/if}}'),
                        template.replace(CONTROL, '${injected}' + CONTROL),
                        template.replace('<script ', '<script src="https://example.invalid/x" '),
                        '<table><tr><td>' + template + '</td></tr></table>'):
            with self.subTest(invalid=invalid[:60]):
                self.assertFalse(self.inspect(DEMO_HTML + invalid)['delete_control_observed'])

    def inspect(self, html=DEMO_HTML + CONTROL, status=200):
        session = Mock()
        session.request.return_value = HttpResponse(status, {}, html)
        login = LoginResult(AuthStatus.VERIFIED, "synthetic", session, "synthetic_account")
        with patch("socket.socket", side_effect=AssertionError("network forbidden")):
            result = check_management(login, "mnt")
        session.request.assert_called_once()
        args = session.request.call_args.args
        self.assertEqual(args, ("GET", "https://gall.dcinside.com/mgallery/board/lists/?id=mnt"))
        self.assertFalse(result["server_delete_verified"])
        return result

    def test_enabled_management_button_is_only_ui_evidence(self):
        result = self.inspect()
        self.assertEqual(result["status"], "control_observed")
        self.assertTrue(result["delete_control_observed"])

    def test_unverified_login_does_not_request_board(self):
        for status in (AuthStatus.UNSUPPORTED, AuthStatus.ACCEPTED_UNVERIFIED):
            session = Mock()
            result = check_management(LoginResult(status, "synthetic", session), "mnt")
            self.assertEqual(result["status"], "identity_unverified")
            session.request.assert_not_called()

    def test_http_errors_and_redirects_are_not_retried(self):
        for status in (302, 401, 403, 429, 500):
            with self.subTest(status=status):
                result = self.inspect(status=status)
                self.assertEqual(result["http_status"], status)
                self.assertEqual(result["status"], "http_error")
                self.assertFalse(result["delete_control_observed"])

    def test_bad_or_other_gallery_page_is_not_accepted(self):
        for html in ("<h1>Login</h1>" + CONTROL, DEMO_HTML.replace('id=mnt', 'id=other') + CONTROL):
            with self.subTest(html=html[:40]):
                self.assertEqual(self.inspect(html)["status"], "page_unverified")

    def test_missing_disabled_duplicate_or_inert_control_is_not_evidence(self):
        for control in ("", CONTROL.replace('<button ', '<button disabled '),
                        CONTROL.replace('<button ', '<button aria-disabled="true" '),
                        CONTROL + CONTROL, '<template>' + CONTROL + '</template>',
                        CONTROL.replace('useradmin_btnbox', 'unrelated'),
                        CONTROL.replace('<button ', '<button onclick="other()" '),
                        CONTROL.replace('del_list()', 'del_list(); another_action()')):
            with self.subTest(control=control[:70]):
                self.assertFalse(self.inspect(DEMO_HTML + control)["delete_control_observed"])

    def test_control_in_user_table_is_not_evidence(self):
        html = DEMO_HTML + '<table><tr><td>' + CONTROL + '</td></tr></table>'
        self.assertFalse(self.inspect(html)["delete_control_observed"])

    def test_raw_response_and_exception_are_not_reported(self):
        result = self.inspect('<p>synthetic-private-payload</p>')
        self.assertNotIn('synthetic-private-payload', str(result))
        session = Mock()
        session.request.side_effect = RuntimeError('synthetic-private-payload')
        result = check_management(LoginResult(AuthStatus.VERIFIED, 'safe', session, 'synthetic'), 'mnt')
        self.assertEqual(result['status'], 'inspection_failed')
        self.assertNotIn('synthetic-private-payload', str(result))

    def test_invalid_gallery_stops_before_request(self):
        session = Mock()
        with self.assertRaises(ValueError):
            check_management(LoginResult(AuthStatus.VERIFIED, 'safe', session, 'synthetic'), '../mnt')
        session.request.assert_not_called()
