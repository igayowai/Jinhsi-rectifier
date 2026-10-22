"""Single-owner candidate flows; no threads, network, credentials or disk.

DefenseFlow retains the legacy journal contract. RefreshingDefenseFlow uses
short-lived exclusion for live defense. Neither grants authority to delete;
the adapter rechecks scope, permission, freshness and cancellation before POST.
"""
from dataclasses import dataclass, replace
from math import isfinite
from time import monotonic

from .batch_journal import BatchResponse
from .models import validate_gallery
from .page_preview import classify_list_entries
from .diagnostics import record_error


@dataclass(frozen=True)
class CandidateBatch:
    gallery: str
    post_ids: tuple[int, ...]


@dataclass(frozen=True)
class BatchPartResult:
    post_ids: tuple[int, ...]
    response: BatchResponse
    attempted: bool


@dataclass(frozen=True)
class FlowStats:
    queued: int
    in_flight: int
    attempted: int
    accepted_unverified: int
    confirmed_deleted: int
    stopped: str | None
    unknown_responses: int = 0


class DefenseFlow:
    """Prepare fresh guest batches without waiting for management-log review.

    SUCCESS_REPORTED advances scheduling only. It never increments confirmed
    deletions. Uncertain/rejected responses latch a stop; no automatic resend.
    The retained ID set has a hard bound and is never evicted to permit reuse.
    """

    def __init__(self, gallery, *, batch_size=50, queue_limit=5000,
                 tracked_limit=100000, max_age=3.0, clock=monotonic,
                 include_recommended=False, include_new_members=False):
        self.gallery = validate_gallery(gallery)
        if (type(batch_size) is not int or not 1 <= batch_size <= 250
                or type(queue_limit) is not int or not batch_size <= queue_limit <= 100000
                or type(tracked_limit) is not int or not batch_size <= tracked_limit <= 1000000
                or type(max_age) not in (int, float) or not 0 < max_age <= 60):
            raise ValueError('방어 후보 흐름 설정 오류')
        if type(include_recommended) is not bool:
            raise ValueError('개념글 포함 설정 오류')
        self.include_recommended = include_recommended
        if type(include_new_members) is not bool:
            raise ValueError('신규 회원 포함 설정 오류')
        self.include_new_members = include_new_members
        self.batch_size = batch_size
        self.queue_limit = queue_limit
        self.tracked_limit = tracked_limit
        self.max_age = max_age
        self._clock = clock
        self._pending = {}  # Insertion order is the first-observed queue order.
        self._attempted = set()
        self._in_flight = None
        self._accepted = 0
        self._stopped = None

    @property
    def stats(self):
        return FlowStats(len(self._pending),
                         len(self._in_flight.post_ids) if self._in_flight else 0,
                         len(self._attempted), self._accepted, 0, self._stopped)

    def stop(self, reason='user_stopped'):
        if reason not in {'user_stopped', 'auth_required', 'forbidden', 'rate_limited',
                          'quota_exhausted', 'read_failed', 'server_error', 'empty_response',
                          'storage_failed',
                          'stale_listing', 'invalid_listing', 'scope_mismatch',
                          'rejection_reported'}:
            raise ValueError('방어 중지 사유 오류')
        self._halt(reason)

    def _halt(self, reason):
        # Preserve the first reason even when a delayed response arrives.
        if self._stopped is None:
            self._stopped = reason

    def observe(self, entries, *, replace_pending=False):
        """Accept a completed, parsed list snapshot, not raw HTML or instructions.

        The caller supplies all observations of a page group together so the
        existing classifier can detect conflicting metadata across pages.
        Reobserving a queued ID updates its freshness without changing its order.
        """
        if self._stopped:
            return 0
        try:
            if not isinstance(entries, (tuple, list)) or len(entries) > 1250:
                raise ValueError('목록 관측 범위 오류')
            items = classify_list_entries(self.gallery, entries,
                include_recommended=self.include_recommended,
                include_new_members=self.include_new_members)['items']
        except Exception:
            record_error('defense_flow.observe')
            self._halt('invalid_listing')
            raise ValueError('방어 목록 판별 실패') from None
        now = self._clock()
        pending = {} if replace_pending else dict(self._pending)
        added = 0
        for item in items:
            pid = item['post_id']
            if item['batch_disposition'] != 'candidate':
                pending.pop(pid, None)
            elif pid not in self._attempted:
                added += pid not in pending
                pending[pid] = now
        if len(pending) > self.queue_limit:
            self._halt('queue_capacity')
            return 0
        self._pending = pending
        return added

    @property
    def pending_ids(self):
        return tuple(self._pending)

    def take_batch(self, *, limit=None):
        """Issue a candidate once; no second batch while its response is pending."""
        if self._stopped or self._in_flight or not self._pending:
            return None
        if limit is not None and (type(limit) is not int or not 1 <= limit <= self.batch_size):
            raise ValueError('방어 묶음 크기 오류')
        ids = tuple(self._pending)[:self.batch_size if limit is None else limit]
        now = self._clock()
        if any(not 0 <= now - self._pending[pid] <= self.max_age for pid in ids):
            self._halt('stale_listing')
            return None
        if len(self._attempted) + len(ids) > self.tracked_limit:
            self._halt('tracking_capacity')
            return None
        batch = CandidateBatch(self.gallery, ids)
        self._in_flight = batch
        self._attempted.update(ids)
        for pid in ids:
            del self._pending[pid]
        return batch

    def record_response(self, batch, response):
        """Consume one response for the issued object, never an arbitrary ID list.

        A late valid response after stop may update counters but cannot restart
        scheduling. A mismatched/duplicate/malformed receipt fails closed.
        """
        if batch is not self._in_flight or batch is None or not isinstance(response, BatchResponse):
            self._halt('response_mismatch')
            raise ValueError('방어 묶음 응답 불일치')
        self._in_flight = None
        if response is BatchResponse.SUCCESS_REPORTED:
            self._accepted += len(batch.post_ids)
        else:
            self._halt('rejection_reported' if response is BatchResponse.REJECTION_REPORTED
                       else 'response_unknown')


class RefreshingDefenseFlow(DefenseFlow):
    """Exclude in-flight IDs and briefly cool them after any response.

    Expiry only removes an exclusion; a subsequent fresh observation is still
    required to issue that ID again. Counts describe attempts, not unique posts
    or confirmed deletions. No state survives closing the task.
    """

    def __init__(self, gallery, *, cooldown=2.0, **kwargs):
        if (type(cooldown) not in (int, float) or not isfinite(cooldown)
                or not 0.25 <= cooldown <= 60):
            raise ValueError('중복 제외 시간 오류')
        super().__init__(gallery, **kwargs)
        self.cooldown = cooldown
        self._expires = {}
        self._attempt_count = self._unknown_responses = 0

    @property
    def stats(self):
        return replace(super().stats, attempted=self._attempt_count,
                       unknown_responses=self._unknown_responses)

    def observe(self, entries, *, replace_pending=True, observed_at=None):
        if self._stopped:
            return 0
        now = self._clock()
        observed_at = now if observed_at is None else observed_at
        if (type(observed_at) not in (int, float) or not isfinite(observed_at)
                or observed_at > now):
            self._halt('invalid_listing')
            raise ValueError('목록 관측 시각 오류')
        # A prefetched page can outlive the cooldown. Only a read started after
        # expiry can release an ID; consuming an old page is not a new observation.
        expired = [pid for pid, until in self._expires.items() if until <= observed_at]
        for pid in expired:
            del self._expires[pid]
            self._attempted.remove(pid)
        # Never preserve candidates from a preceding page group.
        return super().observe(entries, replace_pending=True)

    def take_batch(self, *, limit=None):
        batch = super().take_batch(limit=limit)
        if batch is not None:
            self._attempt_count += len(batch.post_ids)
        return batch

    def record_response(self, batch, response):
        if batch is not self._in_flight or batch is None or not isinstance(response, BatchResponse):
            self._halt('response_mismatch')
            raise ValueError('방어 묶음 응답 불일치')
        self._in_flight = None
        self._pending.clear()  # A fresh page group is required after every POST.
        expires = self._clock() + self.cooldown
        self._expires.update((pid, expires) for pid in batch.post_ids)
        if response is BatchResponse.SUCCESS_REPORTED:
            self._accepted += len(batch.post_ids)
        elif response is BatchResponse.UNKNOWN:
            self._unknown_responses += 1
        else:
            self._halt('rejection_reported')

    def record_partitioned_response(self, batch, results):
        """Reconcile at most twelve disjoint parts, including late/unsent parts.

        Validate the entire wave before changing any counters. Only the owner
        calls this, after all worker futures have been collected.
        """
        try:
            if batch is None or batch is not self._in_flight or not 1 <= len(results) <= 12:
                raise ValueError
            seen = set()
            for part in results:
                if (not isinstance(part, BatchPartResult) or not part.post_ids
                        or not isinstance(part.response, BatchResponse)
                        or type(part.attempted) is not bool
                        or any(type(pid) is not int for pid in part.post_ids)
                        or len(set(part.post_ids)) != len(part.post_ids)
                        or seen.intersection(part.post_ids)
                        or (not part.attempted and part.response is not BatchResponse.UNKNOWN)):
                    raise ValueError
                seen.update(part.post_ids)
            if seen != set(batch.post_ids):
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            self._halt('response_mismatch')
            raise ValueError('방어 분할 응답 불일치') from None
        self._in_flight = None
        self._pending.clear()
        expires = self._clock() + self.cooldown
        for part in results:
            if not part.attempted:
                self._attempted.difference_update(part.post_ids)
                self._attempt_count -= len(part.post_ids)
                continue
            self._expires.update((pid, expires) for pid in part.post_ids)
            if part.response is BatchResponse.SUCCESS_REPORTED:
                self._accepted += len(part.post_ids)
            elif part.response is BatchResponse.UNKNOWN:
                self._unknown_responses += 1
            else:
                self._halt('rejection_reported')
