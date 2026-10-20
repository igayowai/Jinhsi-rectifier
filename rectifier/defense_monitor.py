"""UI-owned scalar monitoring. Never consulted by the deletion scheduler.

No request hook, account/session reference, post IDs or recovery ledger. The
existing bounded diagnostic writer runs on the UI thread, outside the worker.
"""
from collections import deque
from datetime import datetime, timezone
from time import monotonic

from . import __version__
from .defense_runner import DefenseRunStats
from .performance import REASONS, write_summary


STOP_REASONS = REASONS | frozenset({
    'worker_failed', 'credential_missing', 'response_unknown', 'response_mismatch',
    'tracking_capacity', 'queue_capacity',
    'task_creation_failed', 'task_start_failed',
})
STAGES = frozenset({'starting', 'login', 'captcha', 'baseline', 'running', 'finished'})


class DefenseMonitor:
    def __init__(self, *, clock=monotonic):
        self.clock = clock
        self.started = self.last_save = clock()
        self.history = deque([(self.started, 0, 0)], maxlen=128)
        self.summary = None
        self.finished = self.final_saved = False
        self.log_failed = False
        self.saved_once = False

    def sample(self, stats, updated_at=0.0, *, finished=False, reason=None, stage=None):
        if self.finished:
            return self.summary
        if not isinstance(stats, DefenseRunStats):
            stats = DefenseRunStats(0, 0, 0, 0, 0, None, 0)
        now = self.clock()
        # Keep one sample preceding the window boundary. Polling can be delayed;
        # use the actual denominator rather than calling a long interval 10s.
        while len(self.history) > 1 and self.history[1][0] <= now - 10:
            self.history.popleft()
        baseline = self.history[0]
        span = max(0, now - baseline[0])
        if now - self.history[-1][0] >= .25:
            self.history.append((now, stats.accepted_unverified, stats.requests))
        reason = reason or stats.stopped
        self.summary = dict(
            schema=1, kind='defense', version=__version__,
            stage=stage if stage in STAGES else ('finished' if finished else 'unknown'),
            recorded_at=datetime.now(timezone.utc).isoformat(),
            elapsed_seconds=round(max(0, now - self.started), 3),
            window_seconds=round(span, 3),
            accepted_per_second=round(max(0, stats.accepted_unverified-baseline[1])/span, 2) if span >= 1 else None,
            delete_requests_per_second=round(max(0, stats.requests-baseline[2])/span, 2) if span >= 1 else None,
            scans=stats.scans, delete_requests=stats.requests,
            accepted_unverified=stats.accepted_unverified,
            unknown_responses=stats.unknown_responses,
            last_candidates=stats.last_candidates, last_deferred=stats.last_deferred,
            last_cycle_ms=stats.last_cycle_ms,
            update_age_seconds=round(max(0, now-updated_at), 2) if updated_at > 0 else None,
            stop_reason=reason if reason in STOP_REASONS else ('other' if reason else None),
            finished=bool(finished),
        )
        self.finished = finished
        return self.summary

    def save_if_due(self, *, writer=None, force=False):
        if self.summary is None or self.log_failed or self.final_saved:
            return
        now = self.clock()
        if not force and not self.finished and now-self.last_save < 5:
            return
        self.last_save = now
        try:
            saved = bool((writer or write_summary)(dict(self.summary)))
        except Exception:
            saved = False
        self.saved_once |= saved
        self.log_failed = not saved
        self.final_saved = self.finished

    @property
    def log_status(self):
        if self.log_failed:
            return '요약 기록 실패 · 삭제 동작과 무관'
        if self.final_saved:
            return '종료 요약 기록됨'
        return '5초 간격 요약 기록 중' if self.saved_once else '요약 기록 준비'


def format_monitor(summary):
    rate = summary['accepted_per_second']
    speed = '속도 집계 중' if rate is None else f"최근 {summary['window_seconds']:.1f}초 요청 수락 {rate:.1f}건/초"
    age = summary['update_age_seconds']
    fresh = '집계 시각 미확인' if age is None else f'마지막 집계 {age:.1f}초 전'
    return (f"{speed} · {fresh}\n"
            f"최근 조회: 처리 후보 {summary['last_candidates']}개 · 묶음 미배정 {summary['last_deferred']}개\n"
            f"목록 조회 {summary['scans']}회 · 삭제 요청 {summary['delete_requests']}회 · 요청 수락 누적 {summary['accepted_unverified']}건\n"
            f"응답 불명 {summary['unknown_responses']}회 · 최근 처리 {summary['last_cycle_ms']:.0f}ms")
