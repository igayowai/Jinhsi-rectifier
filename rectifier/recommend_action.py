"""Explicit, finite recommendation release using DC's update_recom(REL).

One selected set, at most one POST, then a bounded list read-back. Never delete,
register, retry, archive contents or infer success from HTTP 200 or absence.
The caller lends its verified session exclusively until execute returns.
"""
from dataclasses import dataclass, field
from threading import Lock
from time import monotonic

from .auth import DEFAULT_RETURN_URL, _AccountMenu
from .defense_runner import DefenseReadError, ListDefenseAdapter
from .management import DESKTOP_HEADERS, _Controls
from .models import validate_gallery, validate_post_id
from .page_preview import classify_list_entries
from .parser import parse_board_listing
from .policy import MAX_MANUAL_TARGETS, validate_manual_pages
from .request_budget import RequestBudget
from .selection import SelectionReview, SelectionRow
from .diagnostics import record_error


RELEASE_URL = 'https://gall.dcinside.com/ajax/minor_manager_board_ajax/set_recommend'
RELEASE_CONTROLS = ("update_recom('REL')", 'update_recom("REL")',
                    "update_recom('REL');", 'update_recom("REL");')


def validate_release_review(review):
    if not isinstance(review, SelectionReview) or review.complete is not True:
        raise ValueError('완료된 조회 필요함')
    validate_gallery(review.gallery_id)
    validate_manual_pages(review.pages)
    if type(review.rows) is not tuple or not 1 <= len(review.rows) <= MAX_MANUAL_TARGETS:
        raise ValueError('개념글 1~250개 선택 필요함')
    for row in review.rows:
        if (not isinstance(row, SelectionRow) or row.post_kind != 'recommended'
                or row.disposition not in {'delete_guest', 'protected_member'}
                or not isinstance(row.title, str) or not row.title.strip()):
            raise ValueError('회원 또는 비회원 개념글만 해제 가능함')
        validate_post_id(row.post_id)
    if len({row.post_id for row in review.rows}) != len(review.rows):
        raise ValueError('선택 번호 중복')


@dataclass(frozen=True)
class ReleaseReceipt:
    attempted: bool
    released: tuple[int, ...] = field(default=(), repr=False)
    remaining: tuple[int, ...] = field(default=(), repr=False)
    unknown: tuple[int, ...] = field(default=(), repr=False)
    stopped: str | None = None


def format_release_result(receipt):
    if not receipt.attempted:
        return '개념글 해제 요청 없음'
    text = (f'해제 상태 확인 {len(receipt.released)}개 · 개념글 유지 {len(receipt.remaining)}개'
            f' · 미확인 {len(receipt.unknown)}개'
            '\n요청 후 같은 범위를 다시 읽은 결과임. 목록에서 사라진 글은 미확인으로 처리함.')
    for ids, label in ((receipt.remaining, '개념글 유지'), (receipt.unknown, '미확인')):
        if ids:
            text += f'\n{label} 글 번호: ' + ', '.join(map(str, ids))
    return text


class RecommendReleaseAction:
    def __init__(self, login_result, review, *, clock=monotonic):
        validate_release_review(review)
        self.review = review
        # Reuse read/status/cancel/budget handling, never its deletion send method.
        self.reader = ListDefenseAdapter(login_result, review.gallery_id, pages=review.pages,
                                         max_age=60, request_budget=RequestBudget(30))
        self.clock = clock
        self._lock = Lock()
        self._used = False

    def _listing(self, cancel, *, require_control):
        entries, excluded_ids = [], set()
        for page in self.review.pages:
            body = self.reader._read(
                f'https://gall.dcinside.com/mgallery/board/lists/?id={self.review.gallery_id}&page={page}',
                cancel, headers=DESKTOP_HEADERS)
            if require_control:
                controls = _Controls(body, onclicks=RELEASE_CONTROLS)
                if not ((controls.buttons == [True] and not controls.templates)
                        or (controls.templates == [True] and not controls.buttons)):
                    raise DefenseReadError('release_control_unverified')
            parsed, excluded = parse_board_listing(body, self.review.gallery_id)
            entries.extend(parsed)
            excluded_ids.update(excluded)
            if len(entries) > 1250:
                raise DefenseReadError('invalid_listing')
        current = {item['post_id']: item for item in
                   classify_list_entries(self.review.gallery_id, entries)['items']}
        return {pid: item for pid, item in current.items() if str(pid) not in excluded_ids}

    def _fresh(self, cancel, started):
        self.reader._check_cancel(cancel)
        if not 0 <= self.clock() - started <= 60:
            raise DefenseReadError('stale_listing')

    def execute(self, cancel):
        if not self._lock.acquire(blocking=False):
            raise DefenseReadError('already_used')
        ids = tuple(row.post_id for row in self.review.rows)
        attempted, token = False, None
        try:
            if self._used:
                raise DefenseReadError('already_used')
            self._used = True
            started = self.clock()
            menu = self.reader._read(DEFAULT_RETURN_URL, cancel)
            if _AccountMenu(menu).account_id != self.reader.account:
                raise DefenseReadError('auth_required')
            before = self._listing(cancel, require_control=True)
            for row in self.review.rows:
                item = before.get(row.post_id)
                if (item is None or item['title'] != row.title
                        or item['disposition'] != row.disposition
                        or item['post_kind'] != 'recommended'):
                    raise DefenseReadError('selection_changed')
            self._fresh(cancel, started)
            self.reader._wait_budget(cancel)
            self._fresh(cancel, started)
            token = self.reader._session.cookie_value('ci_c', RELEASE_URL)
            if not token:
                raise DefenseReadError('auth_required')
            self._fresh(cancel, started)
            attempted = True
            try:
                response = self.reader._session.request('POST', RELEASE_URL,
                    form=[('ci_t', token), ('id', self.review.gallery_id),
                          *[('nos[]', str(pid)) for pid in ids],
                          ('_GALLTYPE_', 'M'), ('mode', 'REL')],
                    headers={**DESKTOP_HEADERS, 'X-Requested-With': 'XMLHttpRequest',
                             'Origin': 'https://gall.dcinside.com',
                             'Referer': f'https://gall.dcinside.com/mgallery/board/lists/?id={self.review.gallery_id}&page={self.review.pages[0]}',
                             'Accept': 'application/json, text/javascript, */*; q=0.01'})
            except Exception:
                record_error('recommend_action.execute')
                response = None  # Timeout may follow a committed action; read only.
            finally:
                token = None
            if response is not None and response.status in {401, 403, 429}:
                return ReleaseReceipt(True, unknown=ids, stopped={
                    401: 'auth_required', 403: 'forbidden', 429: 'rate_limited'}[response.status])
            after = self._listing(cancel, require_control=False)
            released, remaining, unknown = [], [], []
            for row in self.review.rows:
                item = after.get(row.post_id)
                if (item is None or item['title'] != row.title
                        or item['disposition'] != row.disposition):
                    unknown.append(row.post_id)
                elif item['post_kind'] == 'ordinary':
                    released.append(row.post_id)
                elif item['post_kind'] == 'recommended':
                    remaining.append(row.post_id)
                else:
                    unknown.append(row.post_id)
            return ReleaseReceipt(True, tuple(released), tuple(remaining), tuple(unknown))
        except DefenseReadError as error:
            return ReleaseReceipt(attempted, unknown=ids if attempted else (), stopped=str(error))
        except Exception:
            record_error('recommend_action.execute')
            return ReleaseReceipt(attempted, unknown=ids if attempted else (), stopped='read_failed')
        finally:
            token = None
            self._lock.release()
