from email.message import Message
from http.cookiejar import Cookie
from io import BytesIO
import socket
import unittest
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import parse_qs
from urllib.request import HTTPCookieProcessor, HTTPSHandler, ProxyHandler, build_opener
from urllib.response import addinfourl

from rectifier.auth import AuthError, AuthStatus, HttpResponse, LOGIN_URL, MemorySession, _NoRedirect, _AccountMenu, login


PASSWORD = "synthetic-password-never-log"
TOKEN = "synthetic-csrf-never-log"
FORM = f'''<meta name="csrf-token" content="{TOKEN}">
<input id="conKey" value="synthetic-con-key">
<input id="r_url" value="https://m.dcinside.com">
<input name="_token" value="synthetic-form-token">
<input name="password" type="password">'''

ACCOUNT_MENU = '''<header class="header"><div class="header-top"><ul id="top_depthbox">
<li><a href="https://m.dcinside.com/gallog/synthetic-account">MY 갤로그</a></li>
<li><a href="https://msign.dcinside.com/logout?r_url=https%3A%2F%2Fm.dcinside.com">로그아웃</a></li>
</ul></div></header>'''


class FakeResponse(BytesIO):
    def __init__(self, status=200, body="", headers=None):
        super().__init__(body.encode("utf-8"))
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = "text/html; charset=UTF-8"
        for key, value in (headers or {}).items():
            self.headers[key] = value


class FakeOpener:
    def __init__(self, steps):
        self.steps = list(steps)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        if not self.steps:
            raise AssertionError("Unexpected request in offline fixture")
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step(request) if callable(step) else step


class FixtureHttpsHandler(HTTPSHandler):
    """Exercise urllib's real cookie/redirect handlers without socket activity."""
    def __init__(self, steps):
        super().__init__()
        self.steps = list(steps)
        self.requests = []

    def https_open(self, request):
        self.requests.append(request)
        if not self.steps:
            raise AssertionError("Unexpected urllib fixture request")
        status, values, body = self.steps.pop(0)
        headers = Message()
        for key, value in values.items():
            headers[key] = value
        response = addinfourl(BytesIO(body.encode()), headers, request.full_url, status)
        response.msg = "synthetic response"
        return response


def add_login_cookie(session, *, name="dc_m_login", domain=".dcinside.com", path="/"):
    session._cookies.set_cookie(Cookie(
        version=0, name=name, value="synthetic-cookie-never-log", port=None, port_specified=False,
        domain=domain, domain_specified=domain.startswith("."), domain_initial_dot=domain.startswith("."),
        path=path, path_specified=True, secure=True, expires=None, discard=True,
        comment=None, comment_url=None, rest={}, rfc2109=False))


class AuthTests(unittest.TestCase):
    def test_diagnostics_distinguish_missing_and_valid_account_menu(self):
        for html, expected in ((ACCOUNT_MENU, AuthStatus.VERIFIED), ('<h1>anonymous</h1>', AuthStatus.UNSUPPORTED)):
            with self.subTest(expected=expected):
                session, opener = self.session([
                    FakeResponse(body=FORM), FakeResponse(body='{"result":true}'),
                    FakeResponse(302, headers={'Location': 'https://m.dcinside.com/'}),
                    FakeResponse(body=html),
                ])
                result = login('synthetic-account', PASSWORD, session=session)
                self.assertEqual(result.status, expected)
                self.assertEqual([s.stage for s in result.diagnostics],
                                 ['login_form', 'preflight', 'credentials', 'landing', 'identity'])
                self.assertEqual(result.diagnostics[-1].identity_match, expected is AuthStatus.VERIFIED)
                self.assertEqual(len(opener.requests), 4)
                diagnostic = repr(result.diagnostics)
                for secret in (PASSWORD, TOKEN, 'synthetic-account', 'https://'):
                    self.assertNotIn(secret, diagnostic)

    def test_diagnostics_show_interrupted_request_without_raw_exception(self):
        session, opener = self.session([FakeResponse(body=FORM), URLError('synthetic-private-error')])
        result = login('synthetic-account', PASSWORD, session=session)
        self.assertEqual(result.status, AuthStatus.FAILED)
        self.assertEqual(result.diagnostics[-1].stage, 'preflight')
        self.assertIsNone(result.diagnostics[-1].http_status)
        self.assertNotIn('synthetic-private-error', repr(result))
        self.assertTrue(session._closed)

    def test_diagnostics_preserve_sso_sequence_without_redirect_query_values(self):
        session, opener = self.session([
            FakeResponse(body=FORM), FakeResponse(body='{"result":true}'),
            FakeResponse(307, headers={'Location': 'https://sso.dcinside.com/auth/?opaque=synthetic-secret'}),
            FakeResponse(307, headers={'Location': LOGIN_URL}),
            FakeResponse(302, headers={'Location': 'https://m.dcinside.com/'}),
            FakeResponse(body=ACCOUNT_MENU),
        ])
        result = login('synthetic-account', PASSWORD, session=session)
        self.assertEqual(result.status, AuthStatus.VERIFIED)
        self.assertEqual([s.http_status for s in result.diagnostics], [200, 200, 307, 307, 302, 200, 200])
        self.assertNotIn('synthetic-secret', repr(result))

    def setUp(self):
        self.network = patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def session(self, steps):
        opener = FakeOpener(steps)
        session = MemorySession(opener=opener)
        self.addCleanup(session.close)
        return session, opener

    def accepted_session(self):
        session, opener = self.session([
            FakeResponse(body=FORM), FakeResponse(body='{"result":true,"Block_key":"synthetic-new-key"}'),
            FakeResponse(302, headers={"Location": "https://m.dcinside.com/"}),
        ])
        def final(request):
            add_login_cookie(session)
            return FakeResponse(body="<title>synthetic board</title>")
        opener.steps.append(final)
        return session, opener

    def test_acceptance_is_never_verified_identity(self):
        session, opener = self.accepted_session()
        result = login("synthetic-account", PASSWORD, session=session)
        self.assertTrue(result.accepted)
        self.assertIs(result.session, session)
        self.assertFalse(result.identity_verified)
        self.assertIsNone(result.verified_user_id)
        self.assertEqual(len(opener.requests), 4)
        payload = parse_qs(opener.requests[2].data.decode(), keep_blank_values=True)
        self.assertEqual(payload["password"], [PASSWORD])
        self.assertEqual(payload["loginCash"], [""])
        preflight_headers = {key.lower(): value for key, value in opener.requests[1].header_items()}
        form_headers = {key.lower(): value for key, value in opener.requests[2].header_items()}
        self.assertEqual(preflight_headers["x-requested-with"], "XMLHttpRequest")
        self.assertNotIn("x-requested-with", form_headers)
        self.assertIn("text/html", form_headers["accept"])
        self.assertEqual(payload["conKey"], ["synthetic-new-key"])
        self.assertIsNone(opener.requests[3].data)
        self.assertIsNone(opener.requests[3].get_header("X-csrf-token"))
        self.assertNotIn(PASSWORD, repr(result))
        self.assertNotIn(TOKEN, repr(result))

    def test_response_repr_excludes_sensitive_material(self):
        response = HttpResponse(200, {"Set-Cookie": TOKEN}, FORM)
        self.assertNotIn(TOKEN, repr(response))
        self.assertNotIn("con-key", repr(response))

    def test_identity_requires_closed_unique_account_menu(self):
        self.assertEqual(_AccountMenu(ACCOUNT_MENU).account_id, "synthetic-account")
        with_menu_button = ACCOUNT_MENU.replace('</ul>', '<li><a href="javascript:;">메뉴</a></li></ul>')
        self.assertEqual(_AccountMenu(with_menu_button).account_id, "synthetic-account")
        for body in (
                ACCOUNT_MENU.replace('id="top_depthbox"', 'id="other"'),
                ACCOUNT_MENU.replace('<header class="header">', '<div>').replace('</header>', '</div>'),
                ACCOUNT_MENU + ACCOUNT_MENU,
                ACCOUNT_MENU.replace('</ul>', ''),
                ACCOUNT_MENU.replace('/logout?', '/login?'),
                ACCOUNT_MENU.replace('https://m.dcinside.com/gallog/', 'https://evil.invalid/gallog/'),
                ACCOUNT_MENU.replace('id="top_depthbox"', 'id="top_depthbox" id="other"'),
                ACCOUNT_MENU.replace('/gallog/synthetic-account', '/gallog/synthetic-account?other=1')):
            with self.subTest(body=body):
                self.assertIsNone(_AccountMenu(body).account_id)

    def test_verified_identity_does_not_depend_on_cookie_name(self):
        session, opener = self.session([FakeResponse(body=FORM), FakeResponse(body='{"result":true}'),
                                       FakeResponse(302, headers={"Location": "https://m.dcinside.com/"}),
                                       FakeResponse(body=ACCOUNT_MENU)])
        result = login("synthetic-account", PASSWORD, session=session)
        self.assertEqual(result.status, AuthStatus.VERIFIED)
        self.assertTrue(result.identity_verified)
        self.assertEqual(result.verified_user_id, "synthetic-account")
        self.assertTrue(result.accepted)

    def test_foreign_identity_stops_even_with_cookie_signal(self):
        session, opener = self.accepted_session()
        def final(request):
            add_login_cookie(session)
            return FakeResponse(body=ACCOUNT_MENU.replace("synthetic-account", "other-account"))
        opener.steps[-1] = final
        result = login("synthetic-account", PASSWORD, session=session)
        self.assertEqual(result.status, AuthStatus.REJECTED)
        self.assertFalse(result.identity_verified)
        self.assertTrue(session._closed)

    def test_account_markup_on_post_page_cannot_verify_identity(self):
        session, opener = self.session([FakeResponse(body=FORM), FakeResponse(body='{"result":true}'),
                                       FakeResponse(302, headers={"Location": "https://m.dcinside.com/board/example/1"}),
                                       FakeResponse(body=ACCOUNT_MENU)])
        self.assertFalse(login("synthetic-account", PASSWORD, session=session).identity_verified)

    def test_close_clears_cookies_and_prevents_requests(self):
        session, opener = self.session([])
        add_login_cookie(session)
        session.close()
        self.assertFalse(list(session._cookies))
        with self.assertRaises(AuthError):
            session.request("GET", "https://m.dcinside.com")
        self.assertFalse(opener.requests)

    def test_bad_url_and_credential_headers_never_reach_transport(self):
        for url in ["http://msign.dcinside.com/login", "https://msign.dcinside.com.evil.invalid/",
                    "https://evil.invalid/", "https://msign.dcinside.com:444/", "https://user@msign.dcinside.com/",
                    "https://msign.dcinside.com/\n", "https://msign.dcinside.com./", "https://msign.dcinside.com/#fragment"]:
            with self.subTest(url=url):
                session, opener = self.session([])
                with self.assertRaises(AuthError):
                    session.request("GET", url)
                self.assertFalse(opener.requests)
        session, opener = self.session([])
        with self.assertRaises(AuthError):
            session.request("GET", LOGIN_URL, headers={"Cookie": TOKEN})
        self.assertFalse(opener.requests)

    def test_missing_tokens_does_not_submit_password(self):
        session, opener = self.session([FakeResponse(body="<title>unknown login</title>")])
        result = login("synthetic-account", PASSWORD, session=session)
        self.assertEqual(result.status, AuthStatus.UNSUPPORTED)
        self.assertEqual(len(opener.requests), 1)
        self.assertIsNone(result.session)

    def test_form_return_url_outside_allowlist_never_receives_credentials(self):
        session, opener = self.session([FakeResponse(body=FORM.replace("https://m.dcinside.com", "https://evil.invalid"))])
        result = login("synthetic-account", PASSWORD, session=session)
        self.assertFalse(result.accepted)
        self.assertEqual(len(opener.requests), 1)

    def test_preflight_requires_explicit_boolean_success(self):
        for value in ['{}', '{"result":"true"}', '{"result":1}', '{"result":false}', '[]', 'not-json']:
            with self.subTest(value=value):
                session, opener = self.session([FakeResponse(body=FORM), FakeResponse(body=value)])
                result = login("synthetic-account", PASSWORD, session=session)
                self.assertFalse(result.accepted)
                self.assertEqual(len(opener.requests), 2)
                self.assertNotIn(PASSWORD, repr(result))

    def test_captcha_and_extra_auth_stop_without_attempting_solver(self):
        for challenge in ['<input name="captcha">', '<input id="otp">', '<p>추가 인증 필요</p>', '<p>Captcha required</p>']:
            with self.subTest(challenge=challenge):
                session, opener = self.session([FakeResponse(body=FORM + challenge)])
                result = login("synthetic-account", PASSWORD, session=session)
                self.assertEqual(result.status, AuthStatus.UNSUPPORTED)
                self.assertEqual(len(opener.requests), 1)
        session, opener = self.session([FakeResponse(body=FORM), FakeResponse(body='{"result":false,"captcha_required":true}')])
        self.assertEqual(login("synthetic-account", PASSWORD, session=session).status, AuthStatus.UNSUPPORTED)
        self.assertEqual(len(opener.requests), 2)

    def test_password_post_redirect_never_replays_body(self):
        for status in [307, 308]:
            for target in [LOGIN_URL, "https://gall.dcinside.com/sso", "https://evil.invalid/"]:
                with self.subTest(status=status, target=target):
                    session, opener = self.session([FakeResponse(body=FORM), FakeResponse(body='{"result":true}'),
                                                   FakeResponse(status, headers={"Location": target})])
                    result = login("synthetic-account", PASSWORD, session=session)
                    self.assertEqual(result.status, AuthStatus.UNSUPPORTED)
                    self.assertEqual(len(opener.requests), 3)

    def test_external_redirect_is_blocked_before_get(self):
        session, opener = self.session([FakeResponse(body=FORM), FakeResponse(body='{"result":true}'),
                                       FakeResponse(302, headers={"Location": "https://evil.invalid/"})])
        result = login("synthetic-account", PASSWORD, session=session)
        self.assertFalse(result.accepted)
        self.assertEqual(len(opener.requests), 3)

    def test_exact_sso_handoff_replays_form_once_and_drops_csrf_header(self):
        for status in (307, 308):
            session, opener = self.session([
                FakeResponse(body=FORM), FakeResponse(body='{"result":true}'),
                FakeResponse(status, headers={"Location": "https://sso.dcinside.com/auth/?ticket=synthetic"}),
                FakeResponse(302, headers={"Location": "https://m.dcinside.com/"}),
            ])
            def final(request):
                add_login_cookie(session)
                return FakeResponse(body="board")
            opener.steps.append(final)
            result = login("synthetic-account", PASSWORD, session=session)
            self.assertTrue(result.accepted)
            self.assertEqual(opener.requests[2].data, opener.requests[3].data)
            self.assertEqual(opener.requests[3].get_method(), "POST")
            self.assertIsNone(opener.requests[3].get_header("X-csrf-token"))
            self.assertIsNone(opener.requests[4].data)

    def test_sso_wrong_routes_and_repeat_are_blocked(self):
        for target in ("https://sso.dcinside.com/other", "https://sso.dcinside.com/auth/extra",
                       "https://sso.dcinside.com/auth/%2e%2e/", "http://sso.dcinside.com/auth/",
                       "https://sso.dcinside.com.evil.invalid/auth/", ""):
            session, opener = self.session([
                FakeResponse(body=FORM), FakeResponse(body='{"result":true}'),
                FakeResponse(307, headers={"Location": target})])
            self.assertFalse(login("synthetic-account", PASSWORD, session=session).accepted)
            self.assertEqual(len(opener.requests), 3)
            self.assertTrue(session._closed)
        session, opener = self.session([
            FakeResponse(body=FORM), FakeResponse(body='{"result":true}'),
            FakeResponse(307, headers={"Location": "https://sso.dcinside.com/auth/"}),
            FakeResponse(308, headers={"Location": "https://sso.dcinside.com/auth/"})])
        result = login("synthetic-account", PASSWORD, session=session)
        self.assertEqual(result.status, AuthStatus.UNSUPPORTED)
        self.assertEqual(len(opener.requests), 4)
        self.assertTrue(session._closed)

    def test_sso_round_trip_to_login_only_once(self):
        for last in (302, 307):
            session, opener = self.session([
                FakeResponse(body=FORM), FakeResponse(body='{"result":true}'),
                FakeResponse(307, headers={"Location": "https://sso.dcinside.com/auth/?ticket=synthetic"}),
                FakeResponse(307, headers={"Location": LOGIN_URL}),
                FakeResponse(last, headers={"Location": "https://m.dcinside.com/"}),
            ])
            def final(request):
                add_login_cookie(session)
                return FakeResponse(body="board")
            opener.steps.append(final)
            result = login("synthetic-account", PASSWORD, session=session)
            self.assertEqual(result.accepted, last == 302)
            self.assertEqual(len(opener.requests), 6 if last == 302 else 5)
            self.assertEqual(opener.requests[2].data, opener.requests[4].data)
            self.assertEqual(opener.requests[4].full_url, LOGIN_URL)

    def test_campaign_and_challenge_stop_without_followup(self):
        for body, phrase in (
                ('<p>비밀번호 <b>변경</b> 캠페인</p><a>다음에 변경</a>', '비밀번호 변경 안내'),
                ('<img alt="스스로 개인정보를 지키는 간단한 방법! 비밀번호 변경 캠페인"><a>다음에 변경</a>', '비밀번호 변경 안내'),
                ('<input name="captcha_code"><p>코드 입력</p>', 'CAPTCHA')):
            session, opener = self.session([
                FakeResponse(body=FORM), FakeResponse(body='{"result":true}'),
                FakeResponse(307, headers={"Location": "https://sso.dcinside.com/auth/"}),
                FakeResponse(307, headers={"Location": LOGIN_URL}),
                FakeResponse(body=body)])
            result = login("synthetic-account", PASSWORD, session=session)
            self.assertEqual(result.status, AuthStatus.UNSUPPORTED)
            self.assertIn(phrase, result.message)
            self.assertFalse(result.identity_verified)
            self.assertIsNone(result.session)
            self.assertTrue(session._closed)
            self.assertEqual(len(opener.requests), 5)

    def test_notice_preference_is_not_authentication_and_is_memory_only(self):
        session, opener = self.session([])
        session.defer_password_notice()
        self.assertEqual(session.cookie_value("dc_pw_change", LOGIN_URL), "1")
        self.assertEqual(session.cookie_value("dc_m_login", LOGIN_URL), "")
        cookies = list(session._cookies)
        self.assertEqual(len(cookies), 1)
        self.assertTrue(cookies[0].discard)
        self.assertIsNone(cookies[0].expires)
        self.assertTrue(cookies[0].secure)
        session.close()
        self.assertFalse(list(session._cookies))
        with self.assertRaises(AuthError):
            session.defer_password_notice()

    def test_notice_preference_is_applied_before_login_get(self):
        session, opener = self.session([])
        def first(request):
            self.assertEqual(session.cookie_value("dc_pw_change", LOGIN_URL), "1")
            return FakeResponse(body=FORM)
        opener.steps.extend([first, FakeResponse(body='{"result":false}')])
        result = login("synthetic-account", PASSWORD, session=session)
        self.assertEqual(result.status, AuthStatus.REJECTED)
        self.assertFalse(list(session._cookies))

    def test_sso_return_must_be_exact_original_login_url(self):
        for target in ("https://msign.dcinside.com/login?redirect=synthetic", "https://m.dcinside.com/",
                       "https://evil.invalid/", "https://msign.dcinside.com/login/", ""):
            session, opener = self.session([
                FakeResponse(body=FORM), FakeResponse(body='{"result":true}'),
                FakeResponse(307, headers={"Location": "https://sso.dcinside.com/auth/"}),
                FakeResponse(307, headers={"Location": target})])
            self.assertFalse(login("synthetic-account", PASSWORD, session=session).accepted)
            self.assertEqual(len(opener.requests), 4)

    def test_wrong_password_response_is_fixed_and_safe(self):
        session, opener = self.session([FakeResponse(body=FORM), FakeResponse(body='{"result":true}'),
                                       FakeResponse(body=FORM + PASSWORD)])
        result = login("synthetic-account", PASSWORD, session=session)
        self.assertEqual(result.status, AuthStatus.REJECTED)
        self.assertNotIn(PASSWORD, result.message)
        self.assertNotIn(TOKEN, result.message)

    def test_cookie_without_completed_exchange_is_not_accepted(self):
        session, opener = self.session([])
        add_login_cookie(session)
        self.assertEqual(login("synthetic-account", PASSWORD, session=session).status, AuthStatus.UNSUPPORTED)
        self.assertFalse(opener.requests)

    def test_no_auth_cookie_remains_unverified(self):
        session, opener = self.session([FakeResponse(body=FORM), FakeResponse(body='{"result":true}'),
                                       FakeResponse(302, headers={"Location": "https://m.dcinside.com/"}), FakeResponse(body="board")])
        self.assertEqual(login("synthetic-account", PASSWORD, session=session).status, AuthStatus.UNSUPPORTED)

    def test_forbidden_rate_limit_and_network_errors_stop(self):
        for response in [FakeResponse(403), FakeResponse(429), URLError(PASSWORD)]:
            with self.subTest(response=type(response).__name__):
                session, opener = self.session([response])
                result = login("synthetic-account", PASSWORD, session=session)
                self.assertFalse(result.accepted)
                self.assertEqual(len(opener.requests), 1)
                self.assertNotIn(PASSWORD, result.message)

    def test_cookie_lookup_respects_domain_path_and_closed_state(self):
        session, opener = self.session([])
        add_login_cookie(session, domain="msign.dcinside.com", path="/login")
        self.assertEqual(session.cookie_value("dc_m_login", "https://m.dcinside.com/"), "")
        self.assertEqual(session.cookie_value("dc_m_login", "https://msign.dcinside.com/other"), "")
        self.assertTrue(session.cookie_value("dc_m_login", LOGIN_URL))

    def test_real_urllib_cookie_processor_and_no_redirect_are_offline(self):
        handler = FixtureHttpsHandler([
            (200, {"Set-Cookie": "dc_m_login=synthetic; Domain=.dcinside.com; Path=/; Secure"}, "ok"),
            (302, {"Location": "https://evil.invalid/"}, "redirect"),
        ])
        session = MemorySession()
        self.addCleanup(session.close)
        session._opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(session._cookies), _NoRedirect(), handler)
        session.request("GET", LOGIN_URL)
        self.assertEqual(session.cookie_value("dc_m_login", "https://m.dcinside.com/"), "synthetic")
        response = session.request("GET", "https://m.dcinside.com/")
        self.assertEqual(response.status, 302)
        self.assertEqual(len(handler.requests), 2)
        self.assertEqual(handler.requests[1].get_header("Cookie"), "dc_m_login=synthetic")

    def test_conflicting_form_fields_and_excessive_body_stop(self):
        session, opener = self.session([FakeResponse(body=FORM + '<input id="conKey" value="different">')])
        self.assertEqual(login("synthetic-account", PASSWORD, session=session).status, AuthStatus.UNSUPPORTED)
        self.assertEqual(len(opener.requests), 1)
        session, opener = self.session([FakeResponse(body="x" * 2_000_001)])
        result = login("synthetic-account", PASSWORD, session=session)
        self.assertEqual(result.status, AuthStatus.FAILED)
        self.assertEqual(len(opener.requests), 1)

    def test_redirect_loop_is_bounded(self):
        session, opener = self.session([
            FakeResponse(body=FORM), FakeResponse(body='{"result":true}'),
            *[FakeResponse(302, headers={"Location": "https://m.dcinside.com/"}) for _ in range(7)],
        ])
        result = login("synthetic-account", PASSWORD, session=session)
        self.assertEqual(result.status, AuthStatus.UNSUPPORTED)
        self.assertEqual(len(opener.requests), 9)


if __name__ == "__main__":
    unittest.main()
