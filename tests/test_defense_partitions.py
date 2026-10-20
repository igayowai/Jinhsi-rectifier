from dataclasses import replace
from threading import Barrier, Event, Lock, Thread
from concurrent.futures import ThreadPoolExecutor as RealPool
from unittest import TestCase
from unittest.mock import Mock, patch

from rectifier.auth import AuthStatus, DEFAULT_RETURN_URL, LoginResult
from rectifier.batch_journal import BatchResponse
from rectifier.defense_flow import BatchPartResult, CandidateBatch, RefreshingDefenseFlow
from rectifier.defense_runner import DefenseDispatch, DefenseRunner, ListDefenseAdapter
from rectifier.policy import DeletePolicy
from tests.common import ACCOUNT
from tests.common import listing
from tests.test_defense_flow import guest
from tests.common import CONTROL, MENU, response

S = BatchResponse.SUCCESS_REPORTED
U = BatchResponse.UNKNOWN
R = BatchResponse.REJECTION_REPORTED


class PartitionFlowTests(TestCase):
    def setUp(self):
        self.now = 0.
        self.flow = RefreshingDefenseFlow('fixture', clock=lambda: self.now)
        self.flow.observe([guest(i) for i in (1,2,3)])
        self.batch = self.flow.take_batch()

    def test_reversed_responses_count_each_part_and_cooldown(self):
        self.flow.record_partitioned_response(self.batch, (
            BatchPartResult((3,), S, True), BatchPartResult((1,2), S, True)))
        self.assertEqual(self.flow.stats.accepted_unverified, 3)
        self.assertEqual(self.flow.stats.confirmed_deleted, 0)
        self.flow.observe([guest(i) for i in (1,2,3,4)])
        self.assertEqual(self.flow.take_batch().post_ids, (4,))

    def test_partial_success_unknown_reclassifies_only_after_cooldown(self):
        self.flow.record_partitioned_response(self.batch, (
            BatchPartResult((1,2), S, True), BatchPartResult((3,), U, True)))
        self.assertEqual(self.flow.stats.accepted_unverified, 2)
        self.assertEqual(self.flow.stats.unknown_responses, 1)
        self.now = 3
        self.assertIsNone(self.flow.take_batch())
        self.flow.observe([guest(3)])
        self.assertEqual(self.flow.take_batch().post_ids, (3,))

    def test_partial_success_rejection_keeps_success_and_latches_stop(self):
        self.flow.stop('quota_exhausted')
        self.flow.record_partitioned_response(self.batch, (
            BatchPartResult((3,), S, True), BatchPartResult((1,2), R, True)))
        self.assertEqual(self.flow.stats.accepted_unverified, 1)
        self.assertEqual(self.flow.stats.stopped, 'quota_exhausted')
        self.assertIsNone(self.flow.take_batch())

    def test_unsent_ids_are_not_counted_as_attempts_or_unknown_responses(self):
        self.flow.stop()
        self.flow.record_partitioned_response(self.batch, (
            BatchPartResult((1,2), S, True), BatchPartResult((3,), U, False)))
        self.assertEqual(self.flow.stats.attempted, 2)
        self.assertEqual(self.flow.stats.unknown_responses, 0)
        self.assertEqual(self.flow.stats.in_flight, 0)

    def test_malformed_coverage_or_duplicate_ids_never_change_counters(self):
        cases = [(), (BatchPartResult((1,2),S,True),),
                 (BatchPartResult((1,2),S,True),BatchPartResult((2,3),S,True)),
                 (BatchPartResult((1,2,3,4),S,True),),
                 (BatchPartResult((1,2,3),S,False),),
                 (BatchPartResult((1,2,3),'success',True),),
                 (BatchPartResult((True,2,3),S,True),),
                 (BatchPartResult((1,2,3),S,1),)]
        for parts in cases:
            with self.subTest(parts=parts):
                self.setUp()
                with self.assertRaises(ValueError):
                    self.flow.record_partitioned_response(self.batch, parts)
                self.assertEqual(self.flow.stats.accepted_unverified, 0)
                self.assertEqual(self.flow.stats.in_flight, 3)

    def test_duplicate_or_foreign_batch_receipt_is_rejected(self):
        parts = (BatchPartResult((1,2,3),S,True),)
        with self.assertRaises(ValueError):
            self.flow.record_partitioned_response(replace(self.batch), parts)
        self.setUp()
        self.flow.record_partitioned_response(self.batch, parts)
        with self.assertRaises(ValueError):
            self.flow.record_partitioned_response(self.batch, parts)
        self.assertEqual(self.flow.stats.accepted_unverified, 3)


class PartitionRunnerTests(TestCase):
    def setUp(self):
        denied = patch('socket.socket', side_effect=AssertionError('offline only'))
        denied.start()
        self.addCleanup(denied.stop)
        self.now = 0.
        self.rows = listing((101,102,103,104,105)) + CONTROL
        self.session = Mock()
        self.session.cookie_value.return_value = 'synthetic'
        self.session.request.side_effect = self.request
        self.login = LoginResult(AuthStatus.VERIFIED, 'synthetic', self.session, ACCOUNT)
        self.adapter = ListDefenseAdapter(self.login, 'mnt', clock=lambda: self.now)
        self.runner = DefenseRunner(self.adapter, after_post_id=1, delete_workers=2, clock=lambda: self.now)
        self.posts, self.active, self.maximum = [], 0, 0
        self.lock = Lock()
        self.post_hook = lambda ids: response('{"result":"success"}')

    def request(self, method, url, **kwargs):
        if method == 'GET':
            return response(MENU if url == DEFAULT_RETURN_URL else self.rows)
        ids = tuple(int(v) for k,v in kwargs['form'] if k == 'nos[]')
        with self.lock:
            self.posts.append(ids)
            self.active += 1
            self.maximum = max(self.maximum, self.active)
        try:
            return self.post_hook(ids)
        finally:
            with self.lock:
                self.active -= 1

    def pair_success(self):
        ready = Barrier(2)
        def hook(ids):
            ready.wait(timeout=3)
            return response('{"result":"success"}')
        self.post_hook = hook

    def test_disjoint_uneven_pair_overlaps_and_next_scan_excludes_both(self):
        self.pair_success()
        stats = self.runner.step()
        self.assertEqual(self.maximum, 2)
        self.assertEqual(set(self.posts), {(101,102,103),(104,105)})
        self.assertEqual(stats.requests, 2)
        self.assertEqual(stats.accepted_unverified, 5)
        self.rows = listing((101,102,103,104,105,106,107)) + CONTROL
        self.runner.step()
        self.assertEqual(set(self.posts[2:]), {(106,),(107,)})

    def test_parent_consumed_once_and_child_cannot_send_siblings(self):
        snapshot = self.adapter.read_snapshot(self.runner.cancel)
        d = DefenseDispatch('mnt', ACCOUNT, (101,102,103,104,105))
        children = self.adapter.partition(d, snapshot, self.runner.cancel)
        self.assertFalse(self.adapter.send(d, snapshot, self.runner.cancel).attempted)
        first, second = children
        result = first[0].send(second[1], first[2], self.runner.cancel)
        self.assertFalse(result.attempted)
        self.assertEqual(result.stopped, 'scope_mismatch')
        self.assertFalse(first[0].send(first[1], first[2], self.runner.cancel).attempted)
        self.assertTrue(second[0].send(second[1], second[2], self.runner.cancel).attempted)
        self.assertFalse(second[0].send(second[1], second[2], self.runner.cancel).attempted)

    def test_partial_rate_limit_retains_sibling_success_and_stops(self):
        ready = Barrier(2)
        def hook(ids):
            ready.wait(timeout=3)
            return response('limited',status=429) if ids[0] == 101 else response('{"result":"success"}')
        self.post_hook = hook
        stats = self.runner.step()
        self.assertEqual(stats.stopped, 'rate_limited')
        self.assertEqual(stats.accepted_unverified, 2)
        self.assertEqual(stats.requests, 2)
        self.runner.step()
        self.assertEqual(len(self.posts), 2)

    def test_no_token_stops_unsent_sibling_without_unknown_count(self):
        self.session.cookie_value.return_value = ''
        stats = self.runner.step()
        self.assertEqual(stats.stopped, 'auth_required')
        self.assertEqual(stats.requests, 0)
        self.assertEqual(stats.unknown_responses, 0)
        self.assertEqual(self.runner.flow.stats.attempted, 0)

    def test_child_rechecks_freshness_after_cookie_access(self):
        def cookie(*args):
            self.now = 10
            return 'synthetic'
        self.session.cookie_value.side_effect = cookie
        stats = self.runner.step()
        self.assertEqual(stats.stopped, 'stale_listing')
        self.assertEqual(stats.requests, 0)

    def test_single_candidate_sends_only_one_request(self):
        self.rows = listing((101,)) + CONTROL
        stats = self.runner.step()
        self.assertEqual(self.posts, [(101,)])
        self.assertEqual(stats.accepted_unverified, 1)

    def test_stop_drains_both_posts_before_return(self):
        started, release, finished = Event(), Event(), Event()
        ready = Barrier(2)
        def hook(ids):
            ready.wait(timeout=3)
            started.set()
            if not release.wait(3):
                raise TimeoutError
            return response('{"result":"success"}')
        self.post_hook = hook
        def work():
            try:
                self.runner.step()
            finally:
                finished.set()
        worker = Thread(target=work)
        worker.start()
        try:
            self.assertTrue(started.wait(3))
            self.runner.request_stop()
            self.assertFalse(finished.wait(.1))
        finally:
            release.set()
            worker.join(4)
        self.assertTrue(finished.is_set())
        self.assertEqual(self.runner.stats.stopped, 'user_stopped')
        self.assertEqual(self.runner.stats.accepted_unverified, 5)
        self.assertEqual(self.active, 0)

    def test_unknown_one_part_counts_once_and_does_not_repeat_inflight(self):
        ready = Barrier(2)
        def hook(ids):
            ready.wait(timeout=3)
            if ids[0] == 101:
                raise TimeoutError
            return response('{"result":"success"}')
        self.post_hook = hook
        stats = self.runner.step()
        self.assertEqual(stats.unknown_responses, 1)
        self.assertEqual(stats.accepted_unverified, 2)
        self.assertIsNone(stats.stopped)
        self.runner.step()
        self.assertEqual(len(self.posts), 2)

    def test_member_notice_or_missing_permission_cannot_get_partitions(self):
        for args in ({'writer':'data-uid="member"'}, {'kind':'icon_notice'}):
            self.rows = listing((101,102), **args) + CONTROL
            self.runner.step()
            self.assertEqual(self.posts, [])
        self.rows = listing((101,102))
        self.assertEqual(self.runner.step().stopped, 'forbidden')
        self.assertEqual(self.posts, [])

    def test_invalid_worker_count_and_manual_policy_fail_before_network(self):
        for value in (0,13,True,1.5):
            with self.assertRaises(ValueError):
                DefenseRunner(self.adapter, after_post_id=1, delete_workers=value)
        manual = ListDefenseAdapter(self.login,'mnt',policy=DeletePolicy.MANUAL_SELECTED)
        with self.assertRaises(ValueError):
            DefenseRunner(manual,after_post_id=1,delete_workers=2)
        self.session.request.assert_not_called()

    def test_maximum_batch_is_partitioned_without_dropping_or_duplicating_ids(self):
        self.rows = listing(tuple(range(2,252))) + CONTROL
        self.pair_success()
        stats = self.runner.step()
        self.assertEqual(stats.accepted_unverified, 250)
        self.assertEqual(sorted(map(len,self.posts)), [125,125])
        ids = [pid for part in self.posts for pid in part]
        self.assertEqual(len(set(ids)), 250)

    def test_run_prefetch_overlaps_both_workers_and_reuses_only_new_ids(self):
        self.runner = DefenseRunner(ListDefenseAdapter(self.login, 'mnt'),
                                    after_post_id=1, delete_workers=2, interval=.25)
        ready = Barrier(2)
        def hook(ids):
            ready.wait(timeout=3)
            self.rows = listing((101,102,103,104,105,106,107)) + CONTROL
            self.runner._prefetch[1].result(timeout=3)
            return response('{"result":"success"}')
        self.post_hook = hook
        stats = self.runner.run(lambda s: self.runner.request_stop() if s.requests == 4 else None)
        self.assertEqual(stats.accepted_unverified, 7)
        self.assertEqual(self.maximum, 2)
        self.assertEqual(len(set(pid for part in self.posts for pid in part)), 7)
        self.assertIsNone(self.runner._reader_pool)

    def test_submission_failure_still_collects_already_started_sibling(self):
        first_started, failed_submit = Event(), Event()
        def hook(ids):
            first_started.set()
            if not failed_submit.wait(3):
                raise TimeoutError
            return response('{"result":"success"}')
        self.post_hook = hook
        class FailingPool:
            def __init__(self, **kwargs):
                self.pool = RealPool(**kwargs)
                self.count = 0
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.pool.shutdown(wait=True)
            def submit(self, *args):
                self.count += 1
                if self.count == 2:
                    failed_submit.set()
                    raise RuntimeError('synthetic resource failure')
                future = self.pool.submit(*args)
                if not first_started.wait(3):
                    raise RuntimeError('first worker did not start')
                return future
        with patch('rectifier.defense_runner.ThreadPoolExecutor', FailingPool):
            stats = self.runner.step()
        self.assertEqual(stats.stopped, 'read_failed')
        self.assertEqual(stats.requests, 1)
        self.assertEqual(stats.accepted_unverified, 3)
        self.assertEqual(self.runner.flow.stats.attempted, 3)
        self.assertEqual(self.active, 0)

    def test_callback_stop_preserves_last_actual_cycle_time(self):
        def hook(ids):
            with self.lock:
                self.now += .1
            return response('{"result":"success"}')
        self.post_hook = hook
        stats = self.runner.run(lambda _: self.runner.request_stop())
        self.assertEqual(stats.requests, 2)
        self.assertEqual(stats.last_cycle_ms, 200.)
