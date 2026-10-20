"""Direct login with an in-memory session and mobile account-menu identity check.

Protocol reference (MIT, inspected 2026-09-15; implementation written independently):
https://github.com/GurumNyang/dcinside.js/blob/
d5aabf85165ab94f087b7c7d84eb1d85b5cc1de0/src/auth/mobileLogin.ts
Unknown login steps fail closed; see the README for live verification status.
"""
from dataclasses import dataclass, field
from enum import StrEnum
from html.parser import HTMLParser
from http.cookiejar import Cookie, CookieJar, DefaultCookiePolicy
from http.cookies import CookieError, SimpleCookie
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .diagnostics import record_error


LOGIN_URL = "https://msign.dcinside.com/login"
DEFAULT_RETURN_URL = "https://m.dcinside.com"
ALLOWED_HOSTS = frozenset({"msign.dcinside.com", "m.dcinside.com", "gall.dcinside.com", "www.dcinside.com", "sso.dcinside.com"})
MAX_RESPONSE_BYTES = 2_000_000
USER_AGENT = "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Mobile Safari/537.36"


class AuthStatus(StrEnum):
    VERIFIED = "verified"
    ACCEPTED_UNVERIFIED = "accepted_unverified"
    REJECTED = "rejected"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


class AuthError(RuntimeError):
    """Contains a fixed diagnostic only, never a request or response dump."""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict[str, str] = field(repr=False)
    body: str | bytes = field(repr=False)

    def header(self, name: str) -> str:
        return next((value for key, value in self.headers.items() if key.lower() == name.lower()), "")


def _allowed_url(url: str) -> str:
    try:
        if not isinstance(url, str) or any(ord(char) < 32 for char in url) or "\\" in url:
            raise ValueError
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or parsed.fragment):
            raise ValueError
    except (ValueError, TypeError):
        raise AuthError("허용된 HTTPS 디시 주소가 아니므로 중지함") from None
    return url


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class MemorySession:
    """Own cookie jar. No disk persistence, browser import, or automatic redirects.

    `opener` is a test seam accepting urllib Request objects. Production uses a
    fresh cookie processor and disables environment proxy configuration.
    """

    def __init__(self, *, opener=None):
        self._cookies = CookieJar(policy=DefaultCookiePolicy(
            strict_ns_domain=DefaultCookiePolicy.DomainStrict))
        self._opener = opener if opener is not None else build_opener(
            ProxyHandler({}), HTTPCookieProcessor(self._cookies), _NoRedirect())
        self._closed = False
        self._login_started = False

    def request(self, method: str, url: str, *, form=None, headers=None, binary=False) -> HttpResponse:
        if self._closed:
            raise AuthError("이미 종료된 메모리 세션임")
        _allowed_url(url)
        if method not in {"GET", "POST"} or (method == "GET" and form is not None):
            raise AuthError("지원하지 않는 요청 형식임")
        if binary and method != "GET":
            raise AuthError("이미지 조회에는 GET만 사용 가능함")
        request_headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/json"}
        for key, value in (headers or {}).items():
            if (key.lower() in {"cookie", "authorization", "proxy-authorization", "host"}
                    or any(char in str(key) + str(value) for char in "\r\n")):
                raise AuthError("직접 인증 헤더 주입은 지원하지 않음")
            request_headers[key] = value
        data = None if form is None else urlencode(form).encode("utf-8")
        if data is not None:
            request_headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        request = Request(url, data=data, headers=request_headers, method=method)
        try:
            try:
                response = self._opener.open(request, timeout=15)
            except HTTPError as error:
                response = error  # Redirects/status failures are data, never followed here.
            with response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise AuthError("인증 응답 크기 제한을 초과함")
                charset = response.headers.get_content_charset() or "utf-8"
                body = raw if binary else raw.decode(charset, errors="strict")
                return HttpResponse(response.status, dict(response.headers.items()), body)
        except AuthError:
            raise
        except (URLError, OSError, TimeoutError, UnicodeError, LookupError, ValueError):
            raise AuthError("인증 통신 또는 응답 해석에 실패함") from None

    def cookie_value(self, name: str, url: str) -> str:
        if self._closed:
            raise AuthError("이미 종료된 메모리 세션임")
        request = Request(_allowed_url(url))
        self._cookies.add_cookie_header(request)
        values = SimpleCookie()
        try:
            values.load(request.get_header("Cookie", ""))
        except CookieError:
            raise AuthError("메모리 세션의 쿠키 형식을 해석하지 못함") from None
        return values[name].value if name in values else ""

    def close(self):
        self._cookies.clear()
        self._closed = True

    def defer_password_notice(self):
        """Apply the user's 'next time' choice for this memory session only.

        Official /js/common.min.js afterChange sets dc_pw_change=1. This is
        a notice preference, not an authentication cookie. No persisted expiry.
        """
        if self._closed:
            raise AuthError("이미 종료된 메모리 세션임")
        self._cookies.set_cookie(Cookie(
            version=0, name="dc_pw_change", value="1", port=None, port_specified=False,
            domain=".dcinside.com", domain_specified=True, domain_initial_dot=True,
            path="/", path_specified=True, secure=True, expires=None, discard=True,
            comment=None, comment_url=None, rest={}, rfc2109=False))


@dataclass(frozen=True)
class AuthStep:
    """Fixed stage name and scalar diagnostics; never URLs, headers or HTML."""
    stage: str
    http_status: int | None
    menu_present: bool | None = None
    menu_valid: bool | None = None
    identity_match: bool | None = None


@dataclass(frozen=True)
class LoginResult:
    status: AuthStatus
    message: str
    session: MemorySession | None = field(default=None, repr=False)
    verified_user_id: str | None = None
    diagnostics: tuple[AuthStep, ...] = ()

    @property
    def identity_verified(self) -> bool:
        return self.status is AuthStatus.VERIFIED and self.verified_user_id is not None

    @property
    def accepted(self) -> bool:
        return self.status in {AuthStatus.ACCEPTED_UNVERIFIED, AuthStatus.VERIFIED}


class _AccountMenu(HTMLParser):
    """Identity links only in the mobile homepage's closed header account menu."""
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.headers = self.menus = self.closed_headers = self.closed_menus = 0
        self.ids = []
        self.logout = 0
        self.invalid = False
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        header = tag == "header" and "header" in (a.get("class") or "").split()
        menu = a.get("id") == "top_depthbox"
        inside = any(item[2] for item in self.stack)
        if header:
            self.headers += 1
        if menu:
            self.menus += 1
            if tag != "ul" or not any(item[1] for item in self.stack):
                self.invalid = True
        if (inside or header or menu) and len(attrs) != len(a):
            self.invalid = True
        if inside and tag == "a" and any(part in (a.get("href") or "") for part in ("/gallog/", "/logout")):
            try:
                url = urlsplit(urljoin(DEFAULT_RETURN_URL, a.get("href") or ""))
                if (url.scheme != "https" or url.username or url.password
                        or url.port not in (None, 443) or url.fragment
                        or any(ord(c) < 32 or c == "\\" for c in (a.get("href") or ""))):
                    self.invalid = True
                    return
                match = re.fullmatch(r"/gallog/([a-z0-9_-]{3,64})/?", url.path)
                if match and url.hostname == "m.dcinside.com" and not url.query:
                    self.ids.append(match[1])
                if url.hostname == "msign.dcinside.com" and url.path == "/logout":
                    self.logout += 1
            except ValueError:
                self.invalid = True
        if tag not in {"input", "img", "meta", "link", "br", "hr", "source", "wbr", "area", "embed", "param", "col", "base"}:
            self.stack.append((tag, header, menu))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] != tag:
                continue
            if any(item[2] for item in self.stack[i:]) and i != len(self.stack) - 1:
                self.invalid = True
            for _, header, menu in self.stack[i:]:
                self.closed_headers += int(header)
                self.closed_menus += int(menu)
            del self.stack[i:]
            break

    @property
    def account_id(self):
        if (self.invalid or self.headers != 1 or self.closed_headers != 1
                or self.menus != 1 or self.closed_menus != 1
                or len(self.ids) != 1 or self.logout != 1):
            return None
        return self.ids[0]


class _Stop(Exception):
    def __init__(self, status: AuthStatus, message: str):
        self.status, self.message = status, message


class _LoginDocument(HTMLParser):
    def __init__(self, html: str):
        super().__init__(convert_charrefs=True)
        self.fields = {}
        self.challenge = False
        self.password_field = False
        self.visible_text = []
        self._ignored_depth = 0
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "img" and values.get("alt"):
            self.visible_text.append(values["alt"])
        if tag in {"script", "style"}:
            self._ignored_depth += 1
        name = values.get("name") or ""
        identifier = values.get("id") or ""
        if tag in {"input", "iframe"} and "disabled" not in values:
            signal = " ".join((name, identifier, values.get("src") or "")).lower()
            if any(marker in signal for marker in ("captcha", "twofactor", "two_factor", "otp", "verification_code", "auth_code")):
                self.challenge = True
        if tag == "input" and values.get("type", "").lower() == "password":
            self.password_field = True
        key = None
        value = values.get("value") or ""
        if tag == "meta" and name == "csrf-token":
            key, value = "csrf", values.get("content") or ""
        elif tag == "input" and identifier in {"conKey", "r_url"}:
            key = identifier
        elif tag == "input" and name == "_token":
            key = "_token"
        if key:
            if key in self.fields and self.fields[key] != value:
                raise _Stop(AuthStatus.UNSUPPORTED, "로그인 폼의 중복 필드가 일치하지 않음")
            self.fields[key] = value

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data):
        if not self._ignored_depth:
            self.visible_text.append(data)
        if not self._ignored_depth and any(marker in data.lower() for marker in (
                "추가 인증", "추가인증", "2단계 인증", "인증번호", "보안문자", "자동입력 방지",
                "captcha", "two-factor", "verification code")):
            self.challenge = True


def _document(response: HttpResponse) -> _LoginDocument:
    document = _LoginDocument(response.body)
    if document.challenge:
        raise _Stop(AuthStatus.UNSUPPORTED, "CAPTCHA 또는 추가 인증 단계는 지원하지 않음")
    visible = re.sub(r"\s+", "", "".join(document.visible_text))
    if "비밀번호변경캠페인" in visible and "다음에변경" in visible:
        raise _Stop(AuthStatus.UNSUPPORTED, "디시 비밀번호 변경 안내가 표시됨; 공식 로그인 화면에서 다음에 변경 또는 변경 절차 확인 필요")
    return document


def _ok(response: HttpResponse):
    if response.status in {403, 429}:
        raise _Stop(AuthStatus.UNSUPPORTED, "서버가 인증 요청을 제한함; 자동 재시도 없음")
    if response.status != 200:
        raise _Stop(AuthStatus.UNSUPPORTED, "확인되지 않은 인증 응답 상태임")


def login(account: str, password: str, *, return_url: str = DEFAULT_RETURN_URL,
          session: MemorySession | None = None, captcha_prompt=None) -> LoginResult:
    """Attempt once. VERIFIED checks identity, never gallery deletion authority.

    Caller obtains the password with hidden input. No credential CLI argument,
    environment lookup, file read, persistence, printing, or logging occurs here.
    Python strings cannot promise physical memory zeroization.
    """
    current = session if session is not None else MemorySession()
    accepted = False
    payload = {}
    steps = []
    from .captcha import CaptchaError, from_html, from_preflight, read_image, validate_answer
    from time import monotonic
    captcha_used = False

    def solve(challenge):
        nonlocal captcha_used
        if captcha_used:
            raise _Stop(AuthStatus.UNSUPPORTED, "보안문자가 다시 요구됨; 자동 재시도 없이 중지함")
        if captcha_prompt is None:
            raise _Stop(AuthStatus.UNSUPPORTED, "보안문자 직접 입력 기능이 연결되지 않음")
        captcha_used = True
        started = monotonic()
        image = read_image(current, challenge.source)
        try:
            try:
                entered = captcha_prompt(image)
            except Exception:
                record_error('auth.solve')
                raise CaptchaError('보안문자 입력 처리 실패; 원문은 출력하지 않음') from None
            answer = validate_answer(entered)
        finally:
            image = b''
        if monotonic() - started > 300:
            raise CaptchaError("보안문자 입력 시간이 초과됨")
        steps.append(AuthStep("captcha_input", None))
        return challenge, answer

    def request(stage, method, url, **kwargs):
        steps.append(AuthStep(stage, None))
        response = current.request(method, url, **kwargs)
        steps[-1] = AuthStep(stage, response.status)
        return response

    try:
        if not isinstance(account, str) or not account.strip() or not isinstance(password, str) or not password:
            raise _Stop(AuthStatus.REJECTED, "아이디와 비밀번호 입력이 필요함")
        _allowed_url(return_url)
        if current._closed or current._login_started or any(current._cookies):
            raise _Stop(AuthStatus.UNSUPPORTED, "로그인에는 새 메모리 세션이 필요함")
        current._login_started = True
        # The user selected "다음에 변경". Carry that choice into this fresh
        # application session; completing it in Chrome does not set this jar.
        current.defer_password_notice()
        page_url = LOGIN_URL + "?" + urlencode({"r_url": return_url})
        page = request("login_form", "GET", page_url, headers={"Referer": DEFAULT_RETURN_URL})
        _ok(page)
        initial_challenge = from_html(page.body)
        captcha = solve(initial_challenge) if initial_challenge is not None else None
        fields = (_LoginDocument(page.body) if captcha else _document(page)).fields
        if not fields.get("csrf") or not fields.get("conKey"):
            raise _Stop(AuthStatus.UNSUPPORTED, "확인된 로그인 폼 구조를 찾지 못함")
        def submit(fields, captcha):
            if not fields.get('csrf') or not fields.get('conKey'):
                raise _Stop(AuthStatus.UNSUPPORTED, '보안문자 로그인 폼 구조가 확인되지 않음')
            destination = _allowed_url(fields.get("r_url") or return_url)
            ajax_headers = {"Referer": page_url, "Origin": "https://msign.dcinside.com",
                            "X-CSRF-Token": fields["csrf"], "X-Requested-With": "XMLHttpRequest"}
            for _ in range(2):
                form = [('token_verify', 'dc_login'), ('conKey', fields['conKey']), ('code', account),
                        ('randcode', captcha[0].rand_code if captcha else 'undefined')]
                if captcha:
                    # Official loginRequest uses a repeated code key for the answer.
                    form.append(('code', captcha[1]))
                preflight = request('preflight', 'POST', LOGIN_URL + '/access', headers=ajax_headers, form=form)
                _ok(preflight)
                try:
                    result = json.loads(preflight.body)
                except (ValueError, TypeError):
                    _document(preflight)
                    raise _Stop(AuthStatus.UNSUPPORTED, '로그인 사전 확인 응답 형식이 미확인 상태임') from None
                if not isinstance(result, dict):
                    raise _Stop(AuthStatus.UNSUPPORTED, '로그인 사전 확인 응답 형식이 미확인 상태임')
                challenge = from_preflight(result)
                if challenge is not None:
                    captcha = solve(challenge)
                    continue
                if result.get('reason') == 'wrong_code':
                    raise _Stop(AuthStatus.REJECTED, '입력한 보안문자가 거절됨; 자동 재시도 없음')
                if any(result.get(key) for key in ('captcha', 'captcha_required', 'require_auth', 'two_factor', 'otp')):
                    raise _Stop(AuthStatus.UNSUPPORTED, 'CAPTCHA 또는 추가 인증 단계는 지원하지 않음')
                _document(HttpResponse(200, {}, ' '.join(str(result.get(key, '')) for key in ('message', 'msg', 'cause'))))
                if result.get('result') is False:
                    raise _Stop(AuthStatus.REJECTED, '로그인 사전 확인이 거절됨')
                if result.get('result') is not True:
                    raise _Stop(AuthStatus.UNSUPPORTED, '명시적인 로그인 사전 확인 성공을 받지 못함')
                break
            else:
                raise _Stop(AuthStatus.UNSUPPORTED, '보안문자 확인이 완료되지 않음')
            con_key = result.get('Block_key', fields['conKey'])
            if not isinstance(con_key, str) or not con_key:
                raise _Stop(AuthStatus.UNSUPPORTED, '로그인 사전 확인 키 형식이 미확인 상태임')
            payload.clear()
            payload.update(code=account, password=password, loginCash='', conKey=con_key,
                           r_url=destination, _token=fields.get('_token') or fields['csrf'])
            if captcha:
                payload.update(captchaCode=captcha[1], randCode=captcha[0].rand_code)
            form_headers = {'Referer': page_url, 'Origin': 'https://msign.dcinside.com',
                            'X-CSRF-Token': fields['csrf'], 'Accept': 'text/html,application/xhtml+xml'}
            return request('credentials', 'POST', LOGIN_URL, headers=form_headers, form=payload), form_headers

        response, form_headers = submit(fields, captcha)
        if response.status == 200:
            challenge = from_html(response.body)
            if challenge is not None:
                captcha = solve(challenge)
                response, form_headers = submit(_LoginDocument(response.body).fields, captcha)
        previous_url = LOGIN_URL
        if response.status in {307, 308}:
            # Observed login -> SSO -> login handoff, each edge at most once.
            try:
                target = _allowed_url(urljoin(LOGIN_URL, response.header("Location")))
            except (AuthError, ValueError):
                raise _Stop(AuthStatus.UNSUPPORTED, "확인된 SSO 인증 경로가 아니므로 중지함") from None
            route = urlsplit(target)
            if (not response.header("Location") or route.hostname != "sso.dcinside.com"
                    or route.path != "/auth/"):
                raise _Stop(AuthStatus.UNSUPPORTED, "확인된 SSO 인증 경로가 아니므로 중지함")
            response = request("sso", "POST", target, form=payload, headers={
                "Origin": "https://msign.dcinside.com", "Referer": page_url,
                "Accept": "text/html,application/xhtml+xml"})
            previous_url = target
            if response.status in {307, 308}:
                if urljoin(target, response.header("Location")) != LOGIN_URL:
                    raise _Stop(AuthStatus.UNSUPPORTED, "SSO 반환 주소가 원래 로그인 주소와 다름")
                response = request("sso_return", "POST", LOGIN_URL, form=payload, headers=form_headers)
                previous_url = LOGIN_URL
                if response.status in {307, 308}:
                    raise _Stop(AuthStatus.UNSUPPORTED, "SSO 왕복 후 반복 로그인 이동은 지원하지 않음")
        payload.clear()
        password = ""
        if response.status == 200:
            _document(response)
            raise _Stop(AuthStatus.REJECTED, "로그인이 수락되지 않음; 계정 또는 추가 인증 확인 필요")
        if response.status not in {302, 303}:
            _ok(response)
        for _ in range(6):
            if response.status == 200:
                break
            if response.status not in {302, 303, 307, 308} or not response.header("Location"):
                raise _Stop(AuthStatus.UNSUPPORTED, "확인되지 않은 로그인 리다이렉트임")
            next_url = _allowed_url(urljoin(previous_url, response.header("Location")))
            # No password or CSRF header is carried into a redirected GET.
            response = request("landing", "GET", next_url)
            previous_url = next_url
        _ok(response)
        final = _document(response)
        if final.password_field:
            raise _Stop(AuthStatus.REJECTED, "최종 페이지가 로그인 입력을 다시 요구함")
        landing = urlsplit(previous_url)
        if landing.hostname == "m.dcinside.com" and landing.path in {"", "/"}:
            menu = _AccountMenu(response.body)
            observed_id = menu.account_id
            steps.append(AuthStep("identity", response.status, menu.menus > 0,
                                  observed_id is not None, observed_id == account.lower()))
            if observed_id is not None:
                if observed_id != account.lower():
                    raise _Stop(AuthStatus.REJECTED, "로그인된 계정이 요청한 계정과 다름")
                accepted = True
                return LoginResult(AuthStatus.VERIFIED,
                                   "로그인 계정 확인 완료; 갤러리 삭제 권한은 별도 검증 필요", current, observed_id,
                                   tuple(steps))
        if not any(current.cookie_value(name, DEFAULT_RETURN_URL) for name in (
                "dc_m_login", "m_dcinside", "remember_secret")):
            raise _Stop(AuthStatus.UNSUPPORTED, "로그인 수락 신호를 확인하지 못함")
        accepted = True
        return LoginResult(AuthStatus.ACCEPTED_UNVERIFIED,
                           "로그인 수락 신호 확인됨; 현재 계정과 삭제 권한은 아직 미검증임", current,
                           diagnostics=tuple(steps))
    except _Stop as stop:
        return LoginResult(stop.status, stop.message, diagnostics=tuple(steps))
    except AuthError:
        return LoginResult(AuthStatus.FAILED, "인증 통신 또는 허용 주소 확인에 실패함", diagnostics=tuple(steps))
    except CaptchaError as error:
        return LoginResult(AuthStatus.UNSUPPORTED, str(error), diagnostics=tuple(steps))
    finally:
        payload.clear()
        password = ""
        if not accepted:
            current.close()
