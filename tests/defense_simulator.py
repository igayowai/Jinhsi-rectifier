"""In-process synthetic HTTP responses for the real defense parser and runner.

No sockets, login, credentials, server process, or external writes are needed.
Timing is a configurable assumption, not a model of DCInside internals.
"""
from dataclasses import asdict
from threading import Event, Lock, Timer
from time import monotonic, sleep
from urllib.parse import parse_qs, urlsplit

from rectifier.auth import AuthStatus, DEFAULT_RETURN_URL, LoginResult
from rectifier.defense_runner import DefenseRunner, ListDefenseAdapter
from rectifier.live import DELETE_URL
from tests.test_parser import board, post_row
from tests.common import CONTROL, MENU, response


class SyntheticBoard:
    def __init__(self, *, rate=40, duration=12, initial=0, pages=3,
                 list_delay=.09, account_delay=.04, per_post_delay=.065,
                 base_delete_delay=.02, ghost_seconds=.3, fault=None,
                 clock=monotonic, wait=sleep):
        self.rate, self.duration, self.pages = rate, duration, pages
        self.list_delay, self.account_delay = list_delay, account_delay
        self.per_post_delay, self.base_delete_delay = per_post_delay, base_delete_delay
        self.ghost_seconds, self.fault = ghost_seconds, fault
        self.clock, self.wait = clock, wait
        self.started = clock()
        self.initial = initial
        self.lock = Lock()
        self.rows, self.deleted, self.visible_until, self.observed = {}, {}, {}, set()
        self.generated = 0
        self.calls, self.inflight, self.max_active = [], set(), 0
        self.active = self.gets = self.unsafe = self.duplicates = self.already_deleted = 0
        self.post_entered = Event()
        self._grow()

    def elapsed(self):
        return self.clock()-self.started

    def _grow(self):
        count = self.initial + int(max(0,min(self.duration,self.elapsed())) * self.rate)
        while self.generated < count:
            self.generated += 1
            pid = 1000+self.generated*2
            self.rows[pid] = 'guest'
            if self.generated % 10 == 0:
                self.rows[pid+1] = 'member'

    def _visible(self):
        now = self.elapsed()
        return sorted((pid for pid in self.rows if pid not in self.deleted
                       or self.visible_until.get(pid,0) > now), reverse=True)

    @staticmethod
    def render(rows):
        rendered = [post_row(number='공지', attrs='data-type="icon_notice" data-no="9"',
            link='/mgallery/board/view/?id=mnt&amp;no=9')]
        for pid, kind in rows:
            writer = 'data-uid="synthetic-member"' if kind == 'member' else 'data-uid="" data-ip="192.0.2.1"'
            rendered.append(post_row(number=str(pid), attrs=f'data-type="icon_txt" data-no="{pid}"',
                writer=writer, link=f'/mgallery/board/view/?id=mnt&amp;no={pid}'))
        return board(*rendered)+CONTROL

    def cookie_value(self, *args):
        return 'synthetic-not-a-real-token'

    def request(self, method, url, **kwargs):
        if method == 'GET':
            if url == DEFAULT_RETURN_URL:
                self.wait(self.account_delay)
                return response(MENU.replace('syntheticadmin','syntheticaccount'))
            query = parse_qs(urlsplit(url).query)
            assert url.startswith('https://gall.dcinside.com/mgallery/board/lists/?')
            assert query['id'] == ['mnt']
            page = int(query['page'][0])
            assert 1 <= page <= self.pages
            with self.lock:
                self._grow()
                self.gets += 1
                index = self.gets
                ids = self._visible()[(page-1)*50:page*50]
                self.observed.update(ids)
                rows = [(pid,self.rows[pid]) for pid in ids]
            # Capture at request start: in-flight deletion/new arrivals can age it.
            self.wait(self.list_delay * (1.0,1.2,.8)[index%3])
            return response(self.render(rows))
        assert method == 'POST' and url == DELETE_URL
        assert dict(kwargs['form'])['id'] == 'mnt'
        ids = tuple(int(v) for k,v in kwargs['form'] if k == 'nos[]')
        with self.lock:
            self._grow()
            index = len(self.calls)+1
            record = {'index':index,'ids':ids,'start':self.elapsed()}
            self.calls.append(record)
            self.unsafe += sum(self.rows.get(pid) != 'guest' for pid in ids)
            self.duplicates += len(self.inflight.intersection(ids)) + len(ids)-len(set(ids))
            self.already_deleted += sum(pid in self.deleted for pid in ids)
            self.inflight.update(ids)
            self.active += 1
            self.max_active = max(self.max_active,self.active)
        self.post_entered.set()
        try:
            jitter = (1.,1.08,.92,1.04)[(index-1)%4]
            self.wait((self.base_delete_delay+self.per_post_delay*len(ids))*jitter)
            fault = self.fault if index == 1 else None
            with self.lock:
                record['end'] = self.elapsed()
                if fault not in ('rate_limited','unknown_unapplied'):
                    for pid in ids:
                        if self.rows.get(pid) == 'guest' and pid not in self.deleted:
                            self.deleted[pid] = self.elapsed()
                            self.visible_until[pid] = self.elapsed()+self.ghost_seconds
                record['outcome'] = fault or 'success'
            if fault == 'rate_limited':
                return response('limited',status=429)
            if fault in ('unknown_applied','unknown_unapplied'):
                return response('')
            return response('{"result":"success"}')
        finally:
            with self.lock:
                self.active -= 1
                self.inflight.difference_update(ids)

    def summary(self):
        with self.lock:
            self._grow()
            pending = {pid for pid,kind in self.rows.items() if kind == 'guest' and pid not in self.deleted}
            visible = set(self._visible()[:self.pages*50])
            return {'elapsed':round(self.elapsed(),3),'generated':self.generated,
                    'deleted':len(self.deleted),'backlog':len(pending),
                    'outside_page_window':len(pending-visible),
                    'never_observed_pending':len(pending-self.observed),
                    'protected_targets':self.unsafe,'duplicate_inflight_targets':self.duplicates,
                    'already_deleted_targets':self.already_deleted,'max_active_posts':self.max_active,
                    'active_posts':self.active,'post_requests':len(self.calls),'list_requests':self.gets}


def make_runner(server, *, delete_workers=2, **kwargs):
    # Scheduler scenarios start after a synthetic boundary of 1. Startup boundary
    # capture itself is covered by the runner/task regression tests.
    login = LoginResult(AuthStatus.VERIFIED,'synthetic',server,'syntheticaccount')
    return DefenseRunner(ListDefenseAdapter(login,'mnt',pages=tuple(range(1,server.pages+1))),
                         after_post_id=1,delete_workers=delete_workers,**kwargs)


def run_scenario(*, rate=40, duration=12, drain=12, delete_workers=2, **options):
    server = SyntheticBoard(rate=rate,duration=duration,**options)
    runner = make_runner(server,delete_workers=delete_workers)
    samples = []
    def tick(stats):
        sample = server.summary()
        samples.append(sample)
        if sample['elapsed'] >= duration and sample['backlog'] == 0:
            runner.request_stop()
    timer = Timer(duration+drain,runner.request_stop)
    timer.start()
    try:
        stats = runner.run(tick)
    finally:
        timer.cancel()
        timer.join()
    result = server.summary()
    deleted_during_inflow = sum(t <= duration for t in server.deleted.values())
    result.update(rate=rate,delete_workers=delete_workers,inflow_seconds=duration,drain_budget_seconds=drain,
                  deleted_during_inflow=deleted_during_inflow,
                  backlog_at_inflow_end=int(rate*duration)-deleted_during_inflow,
                  max_backlog=max((s['backlog'] for s in samples),default=0),
                  max_outside_page_window=max((s['outside_page_window'] for s in samples),default=0),
                  completion_order=[p['index'] for p in sorted(server.calls,key=lambda p:p['end'])],
                  stats=asdict(stats),samples=samples,
                  request_sizes=[len(p['ids']) for p in server.calls])
    return result
