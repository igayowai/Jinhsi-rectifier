"""Bounded, sequential moderator bumps; no browser import or persisted state.

Protocol: research/minor_gall_manager.20260915.js:update_bump and
https://github.com/GurumNyang/dcinside.js/blob/master/src/post/managerBump.ts
The implementation is independent. An acknowledgement is not a ranking audit.
"""
from dataclasses import dataclass, replace
from html.parser import HTMLParser
import json
from math import isfinite
from random import uniform
import re
from threading import Event, get_ident
from time import monotonic
from urllib.parse import parse_qsl, urlsplit

from .auth import _AccountMenu, DEFAULT_RETURN_URL
from .management import DESKTOP_HEADERS
from .models import AuthorKind, validate_gallery, validate_post_id
from .parser import parse_board_entries, PostKind
from .post_view import parse_post_view
from .diagnostics import record_error


ORIGIN = 'https://gall.dcinside.com'
BUMP_URL = ORIGIN + '/ajax/minor_manager_board_ajax/update_bump'
SUCCESS_MESSAGE = '끌올되었습니다.'


@dataclass(frozen=True)
class BumpTarget:
    gallery: str
    post_id: int

    def __post_init__(self):
        validate_gallery(self.gallery)
        validate_post_id(self.post_id)

    @property
    def list_url(self):
        return f'{ORIGIN}/mgallery/board/lists/?id={self.gallery}'

    @property
    def url(self):
        return f'{ORIGIN}/mgallery/board/view/?id={self.gallery}&no={self.post_id}'

    @classmethod
    def from_url(cls, value):
        try:
            if not isinstance(value, str) or any(ord(c) < 32 for c in value) or '\\' in value:
                raise ValueError
            url = urlsplit(value.strip())
            if (url.scheme != 'https' or url.hostname != 'gall.dcinside.com'
                    or url.username is not None or url.password is not None
                    or url.port not in (None, 443) or url.fragment
                    or url.path.rstrip('/') != '/mgallery/board/view'):
                raise ValueError
            fields = parse_qsl(url.query, keep_blank_values=True)
            gallery = [v for k, v in fields if k == 'id']
            ids = [v for k, v in fields if k == 'no']
            if len(gallery) != 1 or len(ids) != 1 or not re.fullmatch(r'[1-9][0-9]{0,19}', ids[0]):
                raise ValueError
            return cls(gallery[0], int(ids[0]))
        except (ValueError, TypeError):
            raise ValueError('마이너 갤러리 글의 HTTPS 주소를 입력해야 함') from None


@dataclass(frozen=True)
class BumpLimits:
    interval: float = .5
    duration: float = 10
    max_requests: int = 1
    jitter: float = 0

    def __post_init__(self):
        for value, low, high in ((self.interval, .5, 1800), (self.duration, .5, 18000),
                                 (self.jitter, 0, .2)):
            if type(value) not in (int, float) or not isfinite(value) or not low <= value <= high:
                raise ValueError('간격 0.5~1800초, 실행 시간 0.5~18000초, 추가 지연 0~0.2초 확인 필요')
        if type(self.max_requests) is not int or not 1 <= self.max_requests <= 36000:
            raise ValueError('요청 상한은 1~36000회여야 함')


class BumpStopped(RuntimeError):
    """Only a locally defined diagnostic code may be published."""


class _BumpControls(HTMLParser):
    """Match the list's manager button, not text in articles or executable JS."""
    def __init__(self, html, *, template=True):
        super().__init__(convert_charrefs=True)
        self.divs, self.inert = [], []
        self.tables = 0
        self.buttons, self.templates = [], []
        self.template_text = None
        self.allow_template = template
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        bad = len(a) != len(attrs)
        if (self.allow_template and not self.inert and not self.tables
                and tag == 'script' and a.get('id') == 'minor_buttons-tmpl'):
            self.templates.append(False)
            if not bad and a.get('type') == 'text/x-jquery-tmpl' and 'src' not in a:
                self.template_text = []
        if tag in {'script', 'style', 'template', 'noscript'}:
            self.inert.append(tag)
        if self.inert:
            return
        if tag == 'table':
            self.tables += 1
        if tag == 'div':
            self.divs.append(set() if bad else set((a.get('class') or '').split()))
        if (tag == 'button' and not self.tables and len(self.divs) >= 2
                and 'useradmin_btnbox' in self.divs[-1]
                and 'list_bottom_btnbox' in self.divs[-2]
                and re.fullmatch(r'\s*update_bump\(\)\s*;?\s*', a.get('onclick') or '')):
            self.buttons.append(not bad and 'disabled' not in a
                                and a.get('aria-disabled', 'false') == 'false')

    def handle_endtag(self, tag):
        if self.inert:
            if tag == self.inert[-1]:
                self.inert.pop()
                if tag == 'script' and self.template_text is not None:
                    html = ''.join(self.template_text)
                    self.template_text = None
                    if '{{' not in html and '${' not in html:
                        child = _BumpControls('<div class="list_bottom_btnbox">' + html + '</div>',
                                              template=False)
                        self.templates[-1] = child.valid
            return
        if tag == 'div' and self.divs:
            self.divs.pop()
        if tag == 'table' and self.tables:
            self.tables -= 1

    def handle_data(self, data):
        if self.template_text is not None:
            self.template_text.append(data)

    @property
    def valid(self):
        return (not self.inert and not self.tables and not self.divs
                and ((self.buttons == [True] and not self.templates)
                     or (self.templates == [True] and not self.buttons)))


def _http_stop(status):
    return {401: 'auth_required', 403: 'forbidden', 429: 'rate_limited'}.get(status)


def assess_bump_response(response):
    """Never infer success from HTTP 200, empty JSON, or a missing result alone."""
    if reason := _http_stop(response.status):
        return reason
    if response.status != 200:
        return 'response_unknown'

    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError

    try:
        if not isinstance(response.body, str) or len(response.body) > 8192:
            return 'response_unknown'
        data = json.loads(response.body, object_pairs_hook=pairs, parse_constant=invalid_constant)
        if not isinstance(data, dict) or set(data) - {'result', 'msg'}:
            return 'response_unknown'
        if 'result' in data and data['result'] != 'success':
            return 'rejection_reported'
        if data.get('msg') == SUCCESS_MESSAGE:
            return None
        if isinstance(data.get('msg'), str) and data['msg']:
            return 'rejection_reported'
    except (ValueError, TypeError, RecursionError):
        pass
    return 'response_unknown'


@dataclass(frozen=True)
class BumpAttempt:
    attempted: bool
    stop: str | None = None
    started_at: float | None = None


class BumpAdapter:
    """One runner owns the session. Only send() can issue the bump POST."""
    def __init__(self, login_result, target, account, *, clock=monotonic):
        if (not login_result.identity_verified or login_result.session is None
                or login_result.verified_user_id != account):
            raise BumpStopped('auth_required')
        self._session = login_result.session
        self._target, self._account, self._clock = target, account, clock
        self._owner = get_ident()
        self._identity = None
        self._checked_at = None
        self.title = ''

    def _check_owner(self):
        if get_ident() != self._owner:
            raise BumpStopped('worker_failed')

    def _get(self, url, cancel, deadline):
        if cancel.is_set():
            raise BumpStopped('user_stopped')
        if self._clock() >= deadline:
            raise BumpStopped('duration_limit')
        try:
            response = self._session.request('GET', url, headers=DESKTOP_HEADERS)
        except Exception:
            record_error('bump._get')
            raise BumpStopped('read_failed') from None
        if response.status != 200 or not isinstance(response.body, str):
            raise BumpStopped(_http_stop(response.status)
                              or ('server_error' if response.status >= 500 else 'read_failed'))
        return response.body

    def prepare(self, cancel, deadline):
        self._check_owner()
        # Stamp the START of all checks so slow reads cannot extend freshness.
        checked_at = self._clock()
        menu = _AccountMenu(self._get(DEFAULT_RETURN_URL, cancel, deadline))
        if menu.account_id != self._account:
            raise BumpStopped('auth_required')
        html = self._get(self._target.list_url, cancel, deadline)
        try:
            entries = parse_board_entries(html, self._target.gallery,
                                          reject_special_ids=(self._target.post_id,))
            if not entries:
                raise ValueError
        except ValueError:
            raise BumpStopped('invalid_listing') from None
        if not _BumpControls(html).valid:
            raise BumpStopped('forbidden')
        for entry in entries:
            if entry.post.post_id == self._target.post_id and entry.post_kind not in {
                    PostKind.ORDINARY, PostKind.RECOMMENDED}:
                raise BumpStopped('ineligible_target')
        view = self._get(self._target.url, cancel, deadline)
        try:
            snapshot = parse_post_view(view, self._target.gallery, self._target.post_id)
        except ValueError:
            raise BumpStopped('target_unverified') from None
        if snapshot.post.author_kind is AuthorKind.UNKNOWN:
            raise BumpStopped('target_unverified')
        # Reply/view counts may change while a fixed post remains the same.
        identity = (snapshot.post, snapshot.created_at)
        if self._identity is not None and self._identity != identity:
            raise BumpStopped('target_changed')
        self._identity = identity
        self.title = snapshot.post.title
        self._checked_at = checked_at

    def send(self, cancel, deadline):
        self._check_owner()
        if self._checked_at is None or self._clock() - self._checked_at >= 30:
            raise BumpStopped('stale_check')
        if cancel.is_set():
            return BumpAttempt(False, 'user_stopped')
        if self._clock() >= deadline:
            return BumpAttempt(False, 'duration_limit')
        token = None
        try:
            token = self._session.cookie_value('ci_c', BUMP_URL)
            if not token:
                return BumpAttempt(False, 'auth_required')
            if cancel.is_set():
                return BumpAttempt(False, 'user_stopped')
            if self._clock() >= deadline:
                return BumpAttempt(False, 'duration_limit')
            dispatched_at = self._clock()
            try:
                response = self._session.request('POST', BUMP_URL,
                    form=[('ci_t', token), ('id', self._target.gallery), ('_GALLTYPE_', 'M'),
                          ('nos[]', str(self._target.post_id))],
                    headers={**DESKTOP_HEADERS, 'Origin': ORIGIN,
                             'Referer': self._target.list_url, 'X-Requested-With': 'XMLHttpRequest',
                             'Accept': 'application/json, text/javascript, */*; q=0.01'})
                reason = assess_bump_response(response)
            except Exception:
                record_error('bump.send')
                return BumpAttempt(True, 'response_unknown', dispatched_at)
            return BumpAttempt(True, reason, dispatched_at)
        except Exception:
            record_error('bump.send')
            return BumpAttempt(False, 'auth_required')
        finally:
            token = None


@dataclass(frozen=True)
class BumpStats:
    requests: int = 0
    acknowledged: int = 0
    unknown: int = 0
    elapsed: float = 0
    last_ms: float = 0
    stopped: str | None = None

    @property
    def rate(self):
        return self.acknowledged / self.elapsed if self.elapsed > 0 else 0


def validate_bump_pair(first, second):
    if not isinstance(first, BumpTarget) or not isinstance(second, BumpTarget):
        raise ValueError('실험용 글 주소 두 개를 입력해야 함')
    if first.gallery != second.gallery or first == second:
        raise ValueError('같은 갤러리의 서로 다른 두 글을 입력해야 함')


MAX_BUMP_TARGETS = 20


def validate_bump_targets(targets):
    targets = tuple(targets)
    if not 2 <= len(targets) <= MAX_BUMP_TARGETS or any(not isinstance(t, BumpTarget) for t in targets):
        raise ValueError('여러 글은 2~20개까지 등록할 수 있음')
    if len(set(targets)) != len(targets) or len({t.gallery for t in targets}) != 1:
        raise ValueError('같은 갤러리의 서로 다른 글 주소를 입력해야 함')
    return targets


@dataclass(frozen=True)
class BumpPairProgress:
    titles: tuple[str, ...]
    acknowledged: tuple[int, ...]
    last_index: int | None


class MultiBumpAdapter:
    """Fixed targets in input order, one session/runner; any failure stops all."""
    def __init__(self, login_result, targets, account, *, clock=monotonic):
        targets = validate_bump_targets(targets)
        self.adapters = tuple(BumpAdapter(login_result, target, account, clock=clock)
                              for target in targets)
        self._next_index = 0
        self._last_index = None
        self._acknowledged = [0] * len(targets)

    @property
    def progress(self):
        return BumpPairProgress(tuple(a.title for a in self.adapters),
                                tuple(self._acknowledged), self._last_index)

    def prepare(self, cancel, deadline):
        # Verify every target before the first POST and at each common refresh.
        for adapter in self.adapters:
            adapter.prepare(cancel, deadline)

    def send(self, cancel, deadline):
        index = self._next_index
        attempt = self.adapters[index].send(cancel, deadline)
        if attempt.attempted:
            self._last_index = index
            if attempt.stop is None:
                self._acknowledged[index] += 1
                self._next_index = (index + 1) % len(self.adapters)
        return attempt


class AlternatingBumpAdapter(MultiBumpAdapter):
    """Compatibility entry for existing two-post callers."""
    def __init__(self, login_result, first, second, account, *, clock=monotonic):
        super().__init__(login_result, (first, second), account, clock=clock)


class BumpRunner:
    def __init__(self, adapter, limits, *, cancel=None, clock=monotonic, random_uniform=uniform):
        self.adapter, self.limits = adapter, limits
        self.cancel = cancel if cancel is not None else Event()
        self._clock = clock
        self._random_uniform = random_uniform
        self.stats = BumpStats()
        self._used = False

    def _jitter(self):
        if not self.limits.jitter:
            return 0
        value = self._random_uniform(0, self.limits.jitter)
        if not isfinite(value) or not 0 <= value <= self.limits.jitter:
            raise BumpStopped('worker_failed')
        return value

    def run(self, on_progress=lambda stats: None):
        if self._used:
            raise RuntimeError('종료한 끌올 실행을 재사용할 수 없음')
        self._used = True
        started = self._clock()
        deadline = started + self.limits.duration
        refresh_at, next_start = started, started
        reason = 'worker_failed'
        try:
            next_start += self._jitter()
            while True:
                now = self._clock()
                if self.cancel.is_set():
                    reason = 'user_stopped'
                    break
                if now >= deadline:
                    reason = 'duration_limit'
                    break
                if self.stats.requests >= self.limits.max_requests:
                    reason = 'request_limit'
                    break
                if now < next_start:
                    self.cancel.wait(min(next_start - now, deadline - now))
                    continue
                if now >= refresh_at:
                    self.adapter.prepare(self.cancel, deadline)
                    refresh_at = now + 30
                sent_at = self._clock()
                attempt = self.adapter.send(self.cancel, deadline)
                finished = self._clock()
                dispatched_at = attempt.started_at if attempt.started_at is not None else sent_at
                self.stats = replace(self.stats,
                    requests=self.stats.requests + int(attempt.attempted),
                    acknowledged=self.stats.acknowledged + int(attempt.attempted and attempt.stop is None),
                    unknown=self.stats.unknown + int(attempt.attempted and attempt.stop == 'response_unknown'),
                    elapsed=max(0, finished - started), last_ms=max(0, (finished - dispatched_at) * 1000))
                on_progress(self.stats)
                if attempt.stop is not None:
                    reason = attempt.stop
                    break
                # No catch-up schedule, parallel requests, or automatic retry.
                next_start = max(finished, dispatched_at + self.limits.interval) + self._jitter()
        except BumpStopped as exc:
            reason = str(exc)
        finally:
            self.stats = replace(self.stats, elapsed=max(0, self._clock() - started), stopped=reason)
        return self.stats
