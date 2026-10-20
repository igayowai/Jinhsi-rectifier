"""Offline, deterministic pagination experiment; never contacts DCInside.

Virtual milliseconds are model time, not a benchmark of Python or the server.
Each request snapshots the list at send time and returns after modeled latency.
"""
from collections import deque
from dataclasses import asdict, dataclass
import json
from math import ceil
from unittest.mock import patch

from rectifier.parser import parse_board
from rectifier.policy import preview_page_items
from tests.test_parser import board, post_row


@dataclass(frozen=True)
class Scenario:
    rate: int = 40
    duration_ms: int = 10000
    grace_ms: int = 3000
    page_size: int = 50
    pages: int = 1
    cycle_ms: int = 1000
    latency_ms: int = 200
    delete_rate: int = 0  # Independent idealized service; no HTTP implementation.
    failed_requests: tuple[int, ...] = ()
    slow_requests: tuple[tuple[int, int], ...] = ()

    def __post_init__(self):
        for name in ('rate', 'duration_ms', 'page_size', 'pages', 'cycle_ms', 'latency_ms'):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(name)
        for name in ('grace_ms', 'delete_rate'):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise ValueError(name)
        if self.pages > 10 or self.rate > 1000 or self.duration_ms > 60000:
            raise ValueError('experiment bound')
        if any(type(n) is not int or n <= 0 for n in self.failed_requests):
            raise ValueError('failed_requests')
        if any(type(n) is not int or type(delay) is not int or n <= 0 or delay <= 0
               for n, delay in self.slow_requests):
            raise ValueError('slow_requests')


def parsed_page(ids):
    return parse_board(board(*(post_row(
        number=str(n), link=f'/mgallery/board/view/?id=fixture&amp;no={n}')
        for n in ids)), 'fixture')


def latency_summary(values):
    values = sorted(values)
    if not values:
        return {'p50_ms': None, 'p95_ms': None, 'max_ms': None}
    return {'p50_ms': values[ceil(len(values) * .5) - 1],
            'p95_ms': values[ceil(len(values) * .95) - 1], 'max_ms': values[-1]}


def simulate(s: Scenario):
    # Network is forbidden even if an imported parser later starts doing I/O.
    with patch('socket.socket', side_effect=AssertionError('offline experiment')):
        return _simulate(s)


def _simulate(s):
    live, born, seen, deleted = {}, {}, {}, {}
    queue = deque()
    generated = requests = failures = repeated = peak_queue = 0
    next_start = cycle_start = 0
    page = 1
    pending = None
    at_load_end = None
    slow = dict(s.slow_requests)
    for now in range(s.duration_ms + s.grace_ms + 1):
        count = min(now, s.duration_ms) * s.rate // 1000
        while generated < count:
            generated += 1
            live[generated] = None
            born[generated] = now

        # Unused capacity is lost; no accumulation while the queue is empty.
        slots = (now * s.delete_rate // 1000
                 - max(0, now - 1) * s.delete_rate // 1000)
        for _ in range(slots):
            if queue:
                n = queue.popleft()
                live.pop(n, None)
                deleted[n] = now

        if pending is not None and now == pending[0]:
            _, ids, failed = pending
            pending = None
            if failed:
                failures += 1
            else:
                for item in preview_page_items('fixture', parsed_page(ids)):
                    n = item.post_id
                    if n in seen:
                        repeated += 1
                    else:
                        seen[n] = now
                        if item.disposition == 'delete_guest':
                            queue.append(n)
            if page == s.pages:
                page = 1
                next_start = max(now, cycle_start + s.cycle_ms)
            else:
                page += 1
                next_start = now

        if pending is None and now >= next_start:
            if page == 1:
                cycle_start = now
            requests += 1
            start = (page - 1) * s.page_size
            ids = tuple(reversed(live))[start:start + s.page_size]
            pending = (now + slow.get(requests, s.latency_ms), ids,
                       requests in s.failed_requests)
        peak_queue = max(peak_queue, len(queue))
        if now == s.duration_ms:
            at_load_end = {'generated': generated, 'discovered': len(seen),
                           'mock_deleted': len(deleted), 'queued': len(queue),
                           'remaining': generated - len(deleted)}

    missing = sorted(born.keys() - seen.keys())
    return {
        'scenario': asdict(s), 'model_only': True,
        'generated': generated, 'discovered': len(seen), 'missed': len(missing),
        'missed_id_sample': missing[:10], 'mock_deleted': len(deleted),
        'queued': len(queue), 'peak_queue': peak_queue,
        'repeated_observations': repeated, 'requests_started': requests,
        'failed_responses': failures, 'in_flight_at_end': int(pending is not None),
        'all_responses_succeeded': failures == 0,
        'at_load_end': at_load_end,
        'discovery_latency': latency_summary([seen[n] - born[n] for n in seen]),
        'mock_delete_latency': latency_summary([deleted[n] - born[n] for n in deleted]),
    }


def experiments():
    for rate in (30, 40, 60, 100):
        for pages in (1, 3):
            s = Scenario(rate=rate, pages=pages)
            yield f'rate{rate}_pages{pages}', s
    for pages in (1, 3):
        yield f'slow_pages{pages}', Scenario(pages=pages, slow_requests=((4, 2500),))
        yield f'failure_pages{pages}', Scenario(pages=pages, failed_requests=(4, 5))
    for delete_rate in (20, 40, 60):
        yield f'delete{delete_rate}', Scenario(pages=3, cycle_ms=250,
                                             delete_rate=delete_rate)


if __name__ == '__main__':
    print(json.dumps({'assumptions': 'Synthetic 10s constant arrivals + 3s grace; '
                      '50 rows/page, snapshot at send, serial reads, idealized independent deletion. '
                      'Latency statistics exclude undiscovered/undeleted posts. '
                      'No live traffic or performance guarantee.',
                      'results': {name: simulate(s) for name, s in experiments()}},
                     ensure_ascii=False, indent=2))
