"""Bound request admissions for one defense run; no I/O or durable history."""
from collections import deque
from math import isfinite
from threading import Event, Lock
from time import monotonic


class BudgetWaitError(ValueError):
    """Only fixed local stop reasons may leave this module."""


class RequestBudget:
    """One shared rolling-second budget for account, list, and delete requests.

Count admission immediately before request preparation; a 5ms margin reduces
scheduling-boundary bursts. Remote arrival timing still depends on the network.
Cancelled admissions are not refunded. Only at most `limit` timestamps remain.
"""
    def __init__(self, limit=30, *, clock=monotonic, wait=None):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('요청 상한 오류')
        self.limit = limit
        self._clock = clock
        self._wait = wait if wait is not None else Event().wait
        self._lock, self._recent = Lock(), deque()

    def acquire(self, cancel, *, deadline=None):
        if deadline is not None and (type(deadline) not in (int, float) or not isfinite(deadline)):
            raise ValueError('요청 대기 기한 오류')
        while True:
            with self._lock:
                now = self._clock()
                if cancel.is_set():
                    raise BudgetWaitError('user_stopped')
                if deadline is not None and now >= deadline:
                    raise BudgetWaitError('stale_listing')
                while self._recent and now - self._recent[0] >= 1.005:
                    self._recent.popleft()
                if len(self._recent) < self.limit:
                    self._recent.append(now)
                    return
                delay = min(.02, max(.0001, self._recent[0] + 1.005 - now))
                if deadline is not None:
                    delay = min(delay, max(.0001, deadline - now))
            self._wait(delay)
