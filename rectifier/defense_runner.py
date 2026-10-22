"""List-based defense with in-memory exclusion and no operation journal.

An owner supplies a verified, exclusive session and stop event. Each dispatch
requires a fresh page group. Server throughput remains unverified.
"""
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed
from math import isfinite
from threading import Event, Lock
from time import monotonic

from .auth import DEFAULT_RETURN_URL, _AccountMenu
from .batch_journal import BatchResponse, _validate_targets
from .defense_flow import BatchPartResult, RefreshingDefenseFlow
from .live import DELETE_URL, _assess_response
from .management import DESKTOP_HEADERS, _Controls
from .models import OutcomeCode
from .page_preview import classify_list_entries
from .parser import PostKind, parse_board_listing
from .policy import MAX_MANUAL_TARGETS, DeletePolicy, validate_manual_pages
from .request_budget import BudgetWaitError, RequestBudget
from .diagnostics import record_error


class DefenseReadError(ValueError):
    """Fixed internal reason only, never remote HTML/messages or exception text."""


@dataclass(frozen=True)
class DefenseSnapshot:
    gallery: str
    account: str
    started_at: float
    entries: tuple = field(repr=False)
    candidates: frozenset[int]


@dataclass(frozen=True)
class DispatchReceipt:
    response: BatchResponse
    attempted: bool
    stopped: str | None = None


@dataclass(frozen=True)
class DefenseDispatch:
    gallery: str
    account: str
    post_ids: tuple[int, ...]


class ListDefenseAdapter:
    """One page group grants one POST or one disjoint, bounded partition.

    Every scan rechecks the account and each page's deletion controls. Freshness
    starts BEFORE those reads, not after slow network work. The server makes
    the final permission decision; page controls are only preflight evidence.
    """
    def __init__(self, login_result, gallery, *, pages=(1,), max_age=3.0, clock=monotonic,
                 policy=DeletePolicy.GUEST_ONLY, request_budget=None,
                 include_recommended=False, include_new_members=False):
        if not login_result.identity_verified or login_result.session is None:
            raise ValueError('확인된 로그인 필요함')
        _validate_targets(gallery, login_result.verified_user_id, (1,))
        if type(max_age) not in (int, float) or not isfinite(max_age) or not 0 < max_age <= 60:
            raise ValueError('목록 유효 시간 오류')
        self.gallery = gallery
        if policy not in (DeletePolicy.GUEST_ONLY, DeletePolicy.MANUAL_SELECTED):
            raise ValueError('삭제 정책 오류')
        if request_budget is not None and not isinstance(request_budget, RequestBudget):
            raise ValueError('요청 상한 오류')
        self.request_budget = request_budget
        self.policy = policy
        if (type(include_recommended) is not bool
                or (include_recommended and policy is not DeletePolicy.GUEST_ONLY)):
            raise ValueError('개념글 포함은 비회원 방어에만 사용함')
        self.include_recommended = include_recommended
        if (type(include_new_members) is not bool
                or (include_new_members and policy is not DeletePolicy.GUEST_ONLY)):
            raise ValueError('신규 회원 옵션은 방어에만 사용함')
        self.include_new_members = include_new_members
        self.account = login_result.verified_user_id
        self.pages = validate_manual_pages(pages)
        self.max_age = max_age
        self._session = login_result.session
        self._clock = clock
        self._snapshot = None
        self.last_response_diagnostics = None
        self.performance = None  # Opt-in for one manual action only.
        self._login_result = login_result

    def new_reader(self):
        """Independent snapshot ownership, same exclusively borrowed session.

        The runner joins readers before returning the session to its owner.
        No cookies are exported/copied and no additional login is performed.
        """
        return ListDefenseAdapter(self._login_result, self.gallery, pages=self.pages,
                                  max_age=self.max_age, clock=self._clock, policy=self.policy,
                                  request_budget=self.request_budget,
                                  include_recommended=self.include_recommended,
                                  include_new_members=self.include_new_members)

    def read_start_boundary(self, cancel):
        # Always use the newest page, even when the scan range is an older page.
        reader = self.new_reader()
        reader.pages = (1,)
        snapshot = reader.read_snapshot(cancel)
        reader.validate(snapshot, cancel)
        if not snapshot.entries:
            raise DefenseReadError('baseline_unavailable')
        # Include members/recommended posts, not only deletion candidates.
        return max(entry.post.post_id for entry in snapshot.entries)

    def _wait_budget(self, cancel, *, deadline=None):
        if self.request_budget is not None:
            try:
                self.request_budget.acquire(cancel, deadline=deadline)
            except BudgetWaitError as error:
                raise DefenseReadError(str(error)) from None
        self._check_cancel(cancel)

    def _request(self, method, url, **kwargs):
        if self.performance is not None:
            return self.performance.request(self._session.request, method, url, **kwargs)
        return self._session.request(method, url, **kwargs)

    @staticmethod
    def _check_cancel(cancel):
        if cancel.is_set():
            raise DefenseReadError('user_stopped')

    def _read(self, url, cancel, *, headers=None):
        self._check_cancel(cancel)
        self._wait_budget(cancel)
        try:
            response = self._request('GET', url, headers=headers)
        except Exception:
            record_error('defense_runner._read')
            raise DefenseReadError('read_failed') from None
        self._check_cancel(cancel)
        if response.status != 200:
            # 5xx is DCInside's own failure (504 = its gateway timed out), not the user's network.
            reason = {401: 'auth_required', 403: 'forbidden', 429: 'rate_limited'}.get(
                response.status, 'server_error' if response.status >= 500 else 'read_failed')
            raise DefenseReadError(reason)
        if not response.body.strip():
            # HTTP 200 with no document cannot establish account or permission.
            raise DefenseReadError('empty_response')
        return response.body

    def read_snapshot(self, cancel):
        self._snapshot = None
        started = self._clock()
        menu = self._read(DEFAULT_RETURN_URL, cancel)
        try:
            if _AccountMenu(menu).account_id != self.account:
                raise DefenseReadError('auth_required')
            entries, excluded_ids = [], set()
            for page in self.pages:
                body = self._read(
                    f'https://gall.dcinside.com/mgallery/board/lists/?id={self.gallery}&page={page}',
                    cancel, headers=DESKTOP_HEADERS)
                controls = _Controls(body)
                allowed = ((controls.buttons == [True] and not controls.templates)
                           or (controls.templates == [True] and not controls.buttons))
                if not allowed:
                    raise DefenseReadError('forbidden')
                parsed, excluded = parse_board_listing(body, self.gallery)
                entries.extend(parsed)
                excluded_ids.update(excluded)
            if len(entries) > 1250:
                raise DefenseReadError('invalid_listing')
            classified = classify_list_entries(self.gallery, entries,
                include_recommended=self.include_recommended,
                include_new_members=self.include_new_members)
            candidates = frozenset(item['post_id'] for item in classified['items']
                                   if item['batch_disposition'] == 'candidate' or (
                                       self.policy is DeletePolicy.MANUAL_SELECTED
                                       and item['disposition'] == 'protected_member'
                                       and item['post_kind'] == 'ordinary'))
            # Keep cross-page notice protection without parsing the HTML again.
            if excluded_ids.intersection(str(pid) for pid in candidates):
                raise DefenseReadError('invalid_listing')
        except DefenseReadError:
            raise
        except Exception:
            record_error('defense_runner.read_snapshot')
            raise DefenseReadError('invalid_listing') from None
        self._check_cancel(cancel)
        if not 0 <= self._clock() - started <= self.max_age:
            raise DefenseReadError('stale_listing')
        self._snapshot = DefenseSnapshot(self.gallery, self.account, started, tuple(entries), candidates)
        return self._snapshot

    def validate(self, snapshot, cancel):
        self._check_cancel(cancel)
        if snapshot is None or snapshot is not self._snapshot:
            raise DefenseReadError('scope_mismatch')
        if not 0 <= self._clock() - snapshot.started_at <= self.max_age:
            raise DefenseReadError('stale_listing')

    def partition(self, reserved, snapshot, cancel, *, workers=2, max_part_size=None,
                  manual_selected=False):
        """Consume parent authority once; each child can send only its own IDs."""
        try:
            self.validate(snapshot, cancel)
            ids = _validate_targets(reserved.gallery, reserved.account, reserved.post_ids,
                                    max_targets=MAX_MANUAL_TARGETS)
            expected_policy = DeletePolicy.MANUAL_SELECTED if manual_selected else DeletePolicy.GUEST_ONLY
            if (type(manual_selected) is not bool or self.policy is not expected_policy or len(ids) < 2
                    or type(workers) is not int or not 2 <= workers <= 12
                    or (reserved.gallery, reserved.account) != (self.gallery, self.account)
                    or not set(ids) <= snapshot.candidates):
                raise DefenseReadError('scope_mismatch')
            count = min(workers, len(ids))
            if max_part_size is not None:
                if (type(max_part_size) is not int or not 1 <= max_part_size <= MAX_MANUAL_TARGETS
                        or len(ids) > workers * max_part_size):
                    raise DefenseReadError('scope_mismatch')
                count = (len(ids) + max_part_size - 1) // max_part_size
            recommended = {e.post.post_id for e in snapshot.entries
                           if e.post_kind is PostKind.RECOMMENDED}
            if self.include_recommended and recommended.intersection(ids):
                # The site's own list UI sends recommended posts individually.
                cap = max_part_size or MAX_MANUAL_TARGETS
                ordinary = tuple(pid for pid in ids if pid not in recommended)
                chunks = [ordinary[i:i + cap] for i in range(0, len(ordinary), cap)]
                chunks.extend((pid,) for pid in ids if pid in recommended)
                if len(chunks) > workers:
                    raise DefenseReadError('scope_mismatch')
            else:
                size, remainder = divmod(len(ids), count)
                chunks, start = [], 0
                for index in range(count):
                    end = start + size + int(index < remainder)
                    chunks.append(ids[start:end])
                    start = end
            children = []
            for part in chunks:
                child = self.new_reader()
                child.performance = self.performance
                child_snapshot = DefenseSnapshot(snapshot.gallery, snapshot.account,
                    snapshot.started_at,
                    tuple(e for e in snapshot.entries if e.post.post_id in part), frozenset(part))
                child._snapshot = child_snapshot
                children.append((child, DefenseDispatch(self.gallery, self.account, part), child_snapshot))
            return tuple(children)
        finally:
            self._snapshot = None

    def send(self, reserved, snapshot, cancel):
        self.last_response_diagnostics = None
        attempted = False
        token = None
        try:
            self.validate(snapshot, cancel)
            ids = _validate_targets(reserved.gallery, reserved.account, reserved.post_ids,
                                    max_targets=MAX_MANUAL_TARGETS)
            if ((reserved.gallery, reserved.account) != (self.gallery, self.account)
                    or not set(ids) <= snapshot.candidates):
                raise DefenseReadError('scope_mismatch')
            if len(ids) > 1 and any(e.post.post_id in ids and e.post_kind is PostKind.RECOMMENDED
                                    for e in snapshot.entries):
                raise DefenseReadError('scope_mismatch')
            self._wait_budget(cancel, deadline=snapshot.started_at + self.max_age)
            self.validate(snapshot, cancel)
            token = self._session.cookie_value('ci_c', DELETE_URL)
            if not token:
                raise DefenseReadError('auth_required')
            # Recheck after cookie access. A stop prevents subsequent POST;
            # an already started POST cannot be undone.
            self.validate(snapshot, cancel)
            self._snapshot = None
            attempted = True
            response = self._request('POST', DELETE_URL,
                form=[('ci_t', token), ('id', self.gallery),
                      *[('nos[]', str(pid)) for pid in ids], ('_GALLTYPE_', 'M')],
                headers={**DESKTOP_HEADERS, 'X-Requested-With': 'XMLHttpRequest',
                         'Origin': 'https://gall.dcinside.com',
                         'Referer': f'https://gall.dcinside.com/mgallery/board/lists/?id={self.gallery}&page={self.pages[0]}',
                         'Accept': 'application/json, text/javascript, */*; q=0.01'})
            code, self.last_response_diagnostics = _assess_response(response)
            if code is OutcomeCode.SUCCESS:
                self.last_response_diagnostics['decision_reason'] = 'batch_success_reported'
                return DispatchReceipt(BatchResponse.SUCCESS_REPORTED, True)
            if code in {OutcomeCode.AUTH_REQUIRED, OutcomeCode.FORBIDDEN,
                        OutcomeCode.RATE_LIMITED, OutcomeCode.QUOTA_EXHAUSTED}:
                return DispatchReceipt(BatchResponse.REJECTION_REPORTED, True, code.value)
            if (code is OutcomeCode.REJECTED or response.status != 200
                    or self.last_response_diagnostics['result_kind'] == 'other'):
                # An explicit non-success result may include a quota refusal.
                # Do not guess the remote message or retry a known refusal.
                return DispatchReceipt(BatchResponse.REJECTION_REPORTED, True, 'rejection_reported')
            return DispatchReceipt(BatchResponse.UNKNOWN, True)
        except DefenseReadError as error:
            return DispatchReceipt(BatchResponse.UNKNOWN, attempted, str(error))
        except Exception:
            record_error('defense_runner.send')
            return DispatchReceipt(BatchResponse.UNKNOWN, attempted,
                                   None if attempted else 'read_failed')
        finally:
            self._snapshot = None
            token = None


@dataclass(frozen=True)
class DefenseRunStats:
    scans: int
    requests: int
    accepted_unverified: int
    confirmed_deleted: int
    queued: int
    stopped: str | None
    last_cycle_ms: float
    unknown_responses: int = 0
    last_candidates: int = 0
    last_deferred: int = 0


class DefenseRunner:
    """One flow owner, up to twelve deletion workers and one bounded read-ahead.

    Each scan replaces candidates with the current page group. Previously issued
    IDs are excluded until their response arrives and cooldown expires. Missing
    or shifted pages cannot grant dispatch. No on-disk state or account rotation.
    """
    def __init__(self, adapter, *, after_post_id, cancel=None, batch_size=MAX_MANUAL_TARGETS, interval=0.5,
                 cooldown=2.0, clock=monotonic, delete_workers=1, posts_per_request=None):
        if (type(after_post_id) is not int or after_post_id < 1
                or not isinstance(adapter, ListDefenseAdapter) or adapter.policy is not DeletePolicy.GUEST_ONLY
                or type(interval) not in (int, float)
                or not isfinite(interval) or not 0.25 <= interval <= 60
                or type(delete_workers) is not int or not 1 <= delete_workers <= 12
                or type(batch_size) is not int or not 1 <= batch_size <= MAX_MANUAL_TARGETS
                or (posts_per_request is not None and (
                    type(posts_per_request) is not int or not 1 <= posts_per_request <= MAX_MANUAL_TARGETS))):
            raise ValueError('방어 실행 설정 오류')
        if posts_per_request is not None:
            batch_size = min(batch_size, delete_workers * posts_per_request)
        self.posts_per_request = posts_per_request
        self.adapter = adapter
        self._after_post_id = after_post_id
        self.flow = RefreshingDefenseFlow(adapter.gallery, cooldown=cooldown,
                                         batch_size=batch_size, max_age=adapter.max_age, clock=clock,
                                         include_recommended=adapter.include_recommended,
                                         include_new_members=adapter.include_new_members)
        self.cancel = cancel if cancel is not None else Event()
        self.interval = interval
        self.delete_workers = delete_workers
        self._clock = clock
        self._owner = Lock()
        self._scans = self._requests = 0
        self._last_ms = 0.0
        self._last_candidates = self._last_deferred = 0
        self._reader_pool = None
        self._prefetch = None

    @property
    def stats(self):
        flow = self.flow.stats
        return DefenseRunStats(self._scans, self._requests, flow.accepted_unverified,
                               flow.confirmed_deleted, flow.queued, flow.stopped, self._last_ms,
                               flow.unknown_responses, self._last_candidates, self._last_deferred)

    def request_stop(self):
        self.cancel.set()

    def step(self):
        if not self._owner.acquire(blocking=False):
            raise RuntimeError('방어 실행기는 동시에 호출할 수 없음')
        started = self._clock()
        try:
            self._step()
        finally:
            self._last_ms = round((self._clock() - started) * 1000, 3)
            self._owner.release()
        return self.stats

    def _step(self):
        if self.cancel.is_set():
            self.flow.stop()
        if self.stats.stopped:
            return
        try:
            snapshot = self._read_current()
            self._scans += 1
            self.adapter.validate(snapshot, self.cancel)
            fresh_entries = tuple(entry for entry in snapshot.entries
                                  if entry.post.post_id > self._after_post_id)
            self.flow.observe(fresh_entries, replace_pending=True,
                              observed_at=snapshot.started_at)
            self.adapter.validate(snapshot, self.cancel)
        except DefenseReadError as error:
            self.flow.stop(str(error))
            return
        except Exception:
            record_error('defense_runner._step')
            self.flow.stop('invalid_listing')
            return
        self._last_candidates = self.flow.stats.queued
        # Reserve only a prefix that fits the request budget for this wave:
        # recommended posts use one request each, ordinary posts share a batch.
        limit = None
        if self.adapter.include_recommended and self.flow.stats.queued:
            recommended = {e.post.post_id for e in snapshot.entries
                           if e.post_kind is PostKind.RECOMMENDED}
            cap = self.posts_per_request or self.flow.batch_size
            ordinary = singles = limit = 0
            for pid in self.flow.pending_ids[:self.flow.batch_size]:
                singles += pid in recommended
                ordinary += pid not in recommended
                if singles + (ordinary + cap - 1) // cap > self.delete_workers:
                    break
                limit += 1
        batch = self.flow.take_batch(limit=limit)
        self._last_deferred = self.flow.stats.queued
        if batch is None:
            return
        dispatch = DefenseDispatch(batch.gallery, self.adapter.account, batch.post_ids)
        if self._reader_pool is not None:
            reader = self.adapter.new_reader()
            self._prefetch = (reader, self._reader_pool.submit(
                self._read_ahead, reader, snapshot.started_at + self.interval))
        if self.delete_workers > 1 and len(batch.post_ids) > 1:
            self._send_partitioned(batch, dispatch, snapshot)
            return
        receipt = self.adapter.send(dispatch, snapshot, self.cancel)
        self._requests += int(receipt.attempted)
        if self.cancel.is_set():
            self.flow.stop()
        elif receipt.stopped:
            self.flow.stop(receipt.stopped)
        if receipt.attempted:
            self.flow.record_response(batch, receipt.response)
        else:
            self.flow.record_partitioned_response(batch, (
                BatchPartResult(batch.post_ids, receipt.response, False),))

    def _send_partitioned(self, batch, dispatch, snapshot):
        abort = Event()
        user_cancel = self.cancel

        class SendCancel:
            def is_set(self):
                return user_cancel.is_set() or abort.is_set()

        stopped, parts = None, []
        try:
            options = {} if self.posts_per_request is None else {'max_part_size': self.posts_per_request}
            children = self.adapter.partition(dispatch, snapshot, self.cancel,
                                              workers=self.delete_workers, **options)
        except DefenseReadError as error:
            self.flow.stop(str(error))
            self.flow.record_partitioned_response(batch, (
                BatchPartResult(batch.post_ids, BatchResponse.UNKNOWN, False),))
            return

        def send(child):
            adapter, reserved, authority = child
            try:
                receipt = adapter.send(reserved, authority, SendCancel())
            except Exception:
                record_error('defense_runner.send')
                # A future must never escape before sibling responses are drained.
                receipt = DispatchReceipt(BatchResponse.UNKNOWN, True, 'read_failed')
            if receipt.stopped:
                abort.set()
            return receipt

        # Only this bounded wave exists; no queue of future deletion batches.
        with ThreadPoolExecutor(max_workers=len(children), thread_name_prefix='rectifier-delete') as pool:
            futures = {}
            for child in children:
                try:
                    futures[pool.submit(send, child)] = child
                except Exception:
                    record_error('defense_runner.send')
                    abort.set()
                    stopped = 'read_failed'
                    parts.append(BatchPartResult(child[1].post_ids, BatchResponse.UNKNOWN, False))
            for future in as_completed(futures):
                child = futures[future]
                receipt = future.result()
                self._requests += int(receipt.attempted)
                if receipt.stopped and receipt.stopped != 'user_stopped' and stopped is None:
                    stopped = receipt.stopped
                parts.append(BatchPartResult(child[1].post_ids, receipt.response, receipt.attempted))
        if stopped:
            self.flow.stop(stopped)
        elif self.cancel.is_set() or abort.is_set():
            self.flow.stop()
        self.flow.record_partitioned_response(batch, tuple(parts))

    def _read_ahead(self, reader, not_before):
        # Bound reads as well as queue depth; quick POSTs cannot speed up polling.
        if self.cancel.wait(max(0, not_before - self._clock())):
            raise DefenseReadError('user_stopped')
        return reader.read_snapshot(self.cancel)

    def _read_current(self):
        if self._prefetch is None:
            return self.adapter.read_snapshot(self.cancel)
        reader, future = self._prefetch
        self._prefetch = None
        snapshot = future.result()
        self.adapter = reader
        # A long POST may outlive the prefetched page's authority to delete.
        # Throw away the entire group and read again, never refresh its timestamp.
        if not 0 <= self._clock() - snapshot.started_at <= reader.max_age:
            return reader.read_snapshot(self.cancel)
        reader.validate(snapshot, self.cancel)
        return snapshot

    def run(self, on_tick=None):
        if not self._owner.acquire(blocking=False):
            raise RuntimeError('방어 실행기는 동시에 호출할 수 없음')
        try:
            self._reader_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='rectifier-list')
            while not self.stats.stopped:
                started = self._clock()
                try:
                    self._step()
                finally:
                    self._last_ms = round((self._clock() - started) * 1000, 3)
                if on_tick is not None:
                    on_tick(self.stats)
                if self.cancel.is_set():
                    self.flow.stop()
                if self.stats.stopped:
                    break
                self.cancel.wait(max(0, self.interval - (self._clock()-started)))
            return self.stats
        finally:
            # Also fail closed if an owner/UI callback raises unexpectedly.
            if not self.stats.stopped:
                self.flow.stop()
            self.cancel.set()
            try:
                if self._prefetch is not None:
                    self._prefetch[1].cancel()
                if self._reader_pool is not None:
                    self._reader_pool.shutdown(wait=True, cancel_futures=True)
            finally:
                self._reader_pool = self._prefetch = None
                self._owner.release()
