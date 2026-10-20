import base64
import json
import socket
from unittest import TestCase
from unittest.mock import Mock, patch
from urllib.parse import parse_qs
from rectifier.auth import AuthStatus, HttpResponse, MemorySession, login
from rectifier.captcha import CaptchaError, from_html, read_image, validate_answer, validate_image
from tests.test_auth import FakeOpener, FakeResponse, FORM, ACCOUNT_MENU, PASSWORD


GIF = base64.b64decode('R0lGODlhAQABAIAAAP///wAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw==')
SOURCE = 'data:image/gif;base64,' + base64.b64encode(GIF).decode()
CAPTCHA_FORM = (FORM.split('<input id="conKey"')[0] + '<form action="/login" method="post">'
    '<input id="code" name="code"><input id="password" name="password" type="password">'
    '<input id="conKey" value="synthetic-new-key">'
    '<input id="randCode" name="randCode" value="synthetic-rand">'
    '<input id="captcha_code" name="captchaCode">'
    f'<img id="captchaCode" src="{SOURCE}"></form>')
LIMIT = json.dumps({'result': False, 'reason': 'limit_login', 'code': SOURCE, 'randCode': 'synthetic-rand'})


class CaptchaTests(TestCase):
    def setUp(self):
        self.network = patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def session(self, steps):
        opener = FakeOpener(steps)
        session = MemorySession(opener=opener)
        self.addCleanup(session.close)
        return session, opener

    def finish(self):
        return [FakeResponse(body='{"result":true}'),
                FakeResponse(302, headers={'Location': 'https://m.dcinside.com/'}),
                FakeResponse(body=ACCOUNT_MENU)]

    def test_preflight_challenge_resumes_same_session_only_after_human_input(self):
        session, opener = self.session([FakeResponse(body=FORM), FakeResponse(body=LIMIT)] + self.finish())
        def answer(image):
            self.assertEqual(image, GIF)
            self.assertEqual(len(opener.requests), 2)
            return 'Ab1234'
        result = login('synthetic-account', PASSWORD, session=session, captcha_prompt=answer)
        self.assertEqual(result.status, AuthStatus.VERIFIED)
        self.assertIs(result.session, session)
        fields = parse_qs(opener.requests[2].data.decode())
        self.assertEqual(fields['code'], ['synthetic-account', 'Ab1234'])
        self.assertEqual(fields['randcode'], ['synthetic-rand'])
        fields = parse_qs(opener.requests[3].data.decode())
        self.assertEqual(fields['captchaCode'], ['Ab1234'])
        self.assertNotIn('Ab1234', repr(result))

    def test_posted_login_form_challenge_refreshes_form_key_in_same_session(self):
        steps = [FakeResponse(body=FORM), FakeResponse(body='{"result":true}'), FakeResponse(body=CAPTCHA_FORM)]
        session, opener = self.session(steps + self.finish())
        result = login('synthetic-account', PASSWORD, session=session, captcha_prompt=lambda _: 'Ab1234')
        self.assertEqual(result.status, AuthStatus.VERIFIED)
        self.assertEqual(parse_qs(opener.requests[3].data.decode())['conKey'], ['synthetic-new-key'])
        self.assertEqual(sum(r.method == 'GET' and '/login' in r.full_url for r in opener.requests), 1)

    def test_initial_form_challenge_prompts_before_credentials(self):
        session, opener = self.session([FakeResponse(body=CAPTCHA_FORM)] + self.finish())
        result = login('synthetic-account', PASSWORD, session=session, captcha_prompt=lambda _: 'Ab1234')
        self.assertEqual(result.status, AuthStatus.VERIFIED)
        self.assertEqual(parse_qs(opener.requests[1].data.decode())['code'], ['synthetic-account', 'Ab1234'])

    def test_cancel_invalid_answer_and_callback_error_close_without_resubmission(self):
        for callback in (lambda _: None, lambda _: 'contains spaces', Mock(side_effect=RuntimeError('synthetic-secret'))):
            session, opener = self.session([FakeResponse(body=FORM), FakeResponse(body=LIMIT)])
            result = login('synthetic-account', PASSWORD, session=session, captcha_prompt=callback)
            self.assertEqual(result.status, AuthStatus.UNSUPPORTED)
            self.assertTrue(session._closed)
            self.assertEqual(len(opener.requests), 2)
            self.assertNotIn('synthetic-secret', repr(result))

    def test_repeated_challenge_and_wrong_answer_stop(self):
        for body in (LIMIT, '{"result":false,"reason":"wrong_code"}'):
            session, opener = self.session([FakeResponse(body=FORM), FakeResponse(body=LIMIT), FakeResponse(body=body)])
            callback = Mock(return_value='Ab1234')
            result = login('synthetic-account', PASSWORD, session=session, captcha_prompt=callback)
            self.assertFalse(result.accepted)
            self.assertEqual(len(opener.requests), 3)
            callback.assert_called_once()
            self.assertTrue(session._closed)

    def test_expired_input_does_not_submit(self):
        session, opener = self.session([FakeResponse(body=FORM), FakeResponse(body=LIMIT)])
        with patch('time.monotonic', side_effect=[1, 302]):
            result = login('synthetic-account', PASSWORD, session=session, captcha_prompt=lambda _: 'Ab1234')
        self.assertEqual(result.status, AuthStatus.UNSUPPORTED)
        self.assertEqual(len(opener.requests), 2)

    def test_unknown_challenge_remains_unsupported(self):
        session, opener = self.session([FakeResponse(body=FORM), FakeResponse(body='{"result":true}'),
                                        FakeResponse(body='<input id="otp_num">')])
        callback = Mock()
        result = login('synthetic-account', PASSWORD, session=session, captcha_prompt=callback)
        self.assertEqual(result.status, AuthStatus.UNSUPPORTED)
        callback.assert_not_called()

    def test_ambiguous_or_untrusted_forms_not_recognized(self):
        for html in (CAPTCHA_FORM + CAPTCHA_FORM, CAPTCHA_FORM.replace('</form>', ''),
                     CAPTCHA_FORM.replace('action="/login"', 'action="https://evil.invalid/login"'),
                     CAPTCHA_FORM.replace('name="captchaCode"', 'name="otp"'),
                     CAPTCHA_FORM.replace('<form ', '<form action="/else" '),
                     '<script>' + CAPTCHA_FORM + '</script>'):
            with self.subTest(html=html[:60]):
                self.assertIsNone(from_html(html))
        self.assertNotIn('synthetic-rand', repr(from_html(CAPTCHA_FORM)))

    def test_image_is_memory_only_and_external_redirect_or_wrong_format_stops(self):
        session = Mock()
        self.assertEqual(read_image(session, SOURCE), GIF)
        session.request.assert_not_called()
        for source in ('http://msign.dcinside.com/captcha', 'https://evil.invalid/captcha',
                       'https://msign.dcinside.com/logout', '//evil.invalid/captcha',
                       'https://msign.dcinside.com/captcha#fragment', 'data:text/html;base64,AAAA'):
            with self.assertRaises(CaptchaError):
                read_image(session, source)
        session.request.assert_not_called()
        session.request.return_value = HttpResponse(302, {'Location': 'https://evil.invalid'}, b'')
        with self.assertRaises(CaptchaError):
            read_image(session, '/captcha?synthetic=1')
        session.request.assert_called_once()

    def test_binary_request_preserves_image_and_does_not_echo_it(self):
        from email.message import Message
        from io import BytesIO
        raw = BytesIO(GIF)
        raw.status = 200
        raw.headers = Message()
        raw.headers['Content-Type'] = 'image/gif'
        session, opener = self.session([raw])
        image = read_image(session, '/captcha')
        self.assertEqual(image, GIF)

    def test_bad_images_are_rejected_before_ui(self):
        for data in (b'', b'<html>synthetic</html>', b'GIF89a' + b'\xff' * 10, b'x' * 1_000_001):
            with self.assertRaises(CaptchaError):
                validate_image(data)
        for answer in (None, '', 'a b', '<script>', '가나다'):
            with self.assertRaises(CaptchaError):
                validate_answer(answer)
