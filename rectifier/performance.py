"""Scalar-only manual-action timings. Never used to decide or retry deletion."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
from threading import Lock
from time import perf_counter

from . import __version__
from .paths import DATA_ROOT

LOG_PATH = DATA_ROOT / 'diagnostics' / 'manual-performance.jsonl'
MAX_BYTES = 1_048_576
BACKUP_COUNT = 2
_WRITE_LOCK = Lock()
STAGES = frozenset({'account', 'list', 'delete', 'snapshot', 'selection'})
RESULTS = frozenset({'not_sent', 'success_reported', 'rejection_reported', 'unknown'})
REASONS = frozenset({'user_stopped', 'auth_required', 'forbidden', 'rate_limited',
    'quota_exhausted', 'read_failed', 'server_error', 'empty_response', 'invalid_listing',
    'stale_listing', 'scope_mismatch', 'selection_changed', 'rejection_reported',
    'preparation_failed', 'already_used', 'response_unknown'})


class _QuietRotatingHandler(RotatingFileHandler):
    failed = False

    def handleError(self, record):
        # No traceback/locals/record dump to stderr, including in pythonw.
        self.failed = True


def write_summary(summary, *, path=LOG_PATH, max_bytes=MAX_BYTES):
    """Best effort, one bounded record, no root logger or background service."""
    handler = None
    try:
        line = json.dumps(summary, ensure_ascii=True, allow_nan=False, separators=(',', ':'))
        if len(line) > 16_384:
            return False
        with _WRITE_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = _QuietRotatingHandler(path, maxBytes=max_bytes,
                                           backupCount=BACKUP_COUNT, encoding='utf-8')
            handler.setFormatter(logging.Formatter('%(message)s'))
            handler.handle(logging.LogRecord('rectifier.performance', logging.INFO,
                                             '', 0, line, (), None))
            saved = not handler.failed
            handler.close()  # Release Windows handle before another writer can rotate.
            handler = None
            return saved
    except Exception:
        return False
    finally:
        if handler is not None:
            try:
                handler.close()
            except Exception:
                pass


class ManualPerformance:
    def __init__(self, selected_count, page_count, *, clock=perf_counter):
        self.selected_count, self.page_count = selected_count, page_count
        self.clock = clock
        self.started = self.ended = self.ui_ready = None
        self.events = []
        self._events_lock = Lock()
        self._delete_start = self._delete_end = None
        self.result_counts = None
        self.result, self.reason = 'not_sent', None
        self.attempted = False
        self.saved = None

    def start(self):
        if self.started is None:
            self.started = self.clock()

    @contextmanager
    def stage(self, name):
        start = self.clock()
        event = {'stage': name, 'http_status': None, 'failed': True}
        try:
            yield event
            event['failed'] = False
        finally:
            end = self.clock()
            event['ms'] = round(max(0, end - start) * 1000, 3)
            with self._events_lock:
                if name in STAGES and len(self.events) < 64:
                    self.events.append(event)
                if name == 'delete':
                    self._delete_start = start if self._delete_start is None else min(self._delete_start, start)
                    self._delete_end = end if self._delete_end is None else max(self._delete_end, end)

    def request(self, call, method, url, **kwargs):
        # Caller supplies only existing fixed endpoint URLs. No URL, headers,
        # forms, body, exception text, or session reference enters the record.
        name = 'delete' if method == 'POST' else ('list' if '/board/lists/' in url else 'account')
        with self.stage(name) as event:
            response = call(method, url, **kwargs)
            if type(response.status) is int and 100 <= response.status <= 599:
                event['http_status'] = response.status
            return response

    def complete(self, receipt=None, reason=None):
        self.ended = self.clock()
        if receipt is not None:
            self.attempted = bool(receipt.attempted)
            value = receipt.response.value
            self.result = (value if value in RESULTS else 'unknown') if self.attempted else 'not_sent'
            reason = receipt.stopped
            counts = getattr(receipt, 'result_counts', None)
            keys = ('accepted', 'rejected', 'unknown', 'not_sent')
            if isinstance(counts, dict) and all(type(counts.get(k)) is int and counts[k] >= 0 for k in keys):
                self.result_counts = {k: counts[k] for k in keys}
        self.reason = reason if reason in REASONS else ('other' if reason else None)

    def mark_ui_ready(self):
        self.ui_ready = self.clock()

    def summary(self):
        def elapsed(end, start):
            return round(max(0, end - start) * 1000, 3) if end is not None and start is not None else None
        events = [{key: event[key] for key in ('stage', 'http_status', 'failed', 'ms')}
                  for event in self.events]
        requests = [e for e in events if e['stage'] in {'account', 'list', 'delete'}]
        snapshot_ms = sum(e['ms'] for e in events if e['stage'] == 'snapshot')
        reads_ms = sum(e['ms'] for e in requests if e['stage'] != 'delete')
        return dict(schema=2, version=__version__, recorded_at=datetime.now(timezone.utc).isoformat(),
                    selected_count=self.selected_count, page_count=self.page_count,
                    result=self.result, reason=self.reason,
                    attempted=self.attempted, request_count=len(requests),
                    worker_ms=elapsed(self.ended, self.started),
                    ui_ready_ms=elapsed(self.ui_ready, self.started),
                    ui_after_worker_ms=elapsed(self.ui_ready, self.ended),
                    delete_wall_ms=elapsed(self._delete_end, self._delete_start),
                    result_counts=self.result_counts,
                    list_analysis_ms=round(max(0, snapshot_ms - reads_ms), 3), stages=events)

    def save(self, *, writer=write_summary):
        if self.saved is not None:
            return self.saved
        try:
            self.saved = bool(writer(self.summary()))
        except Exception:
            self.saved = False
        return self.saved
