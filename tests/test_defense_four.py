from concurrent.futures import ThreadPoolExecutor as RealPool
from threading import Barrier, Event, Thread
from time import sleep
from unittest import TestCase
from unittest.mock import Mock, patch

from rectifier.batch_journal import BatchResponse
from rectifier.defense_flow import BatchPartResult, RefreshingDefenseFlow
from rectifier.defense_runner import DefenseDispatch, DefenseSnapshot
from tests.defense_simulator import SyntheticBoard, make_runner
from tests.test_defense_flow import guest


class FourWorkerTests(TestCase):
    def setUp(self):
        denied = patch('socket.socket',side_effect=AssertionError('offline'))
        denied.start()
        self.addCleanup(denied.stop)
        self.server = SyntheticBoard(rate=0,initial=20,pages=2,list_delay=.001,
            account_delay=.001,base_delete_delay=.10,per_post_delay=0,ghost_seconds=0)
        self.runner = make_runner(self.server,delete_workers=4,interval=.25)

    def overlap(self, release=None):
        barrier = Barrier(4)
        def wait(delay):
            if delay >= .09:
                barrier.wait(timeout=3)
                if release is not None:
                    if not release.wait(3):
                        raise TimeoutError('test release')
                else:
                    sleep(delay)
            else:
                sleep(delay)
        self.server.wait = wait

    def test_four_real_requests_overlap_and_protected_posts_survive(self):
        self.overlap()
        result = self.runner.step()
        self.assertEqual(result.accepted_unverified,20)
        self.assertEqual(result.requests,4)
        self.assertEqual(self.server.max_active,4)
        self.assertEqual(self.server.unsafe,0)
        self.assertEqual(self.server.duplicates,0)
        self.assertEqual(sorted(len(p['ids']) for p in self.server.calls),[5,5,5,5])

    def test_small_and_uneven_partitions_cover_ids_once(self):
        for count in (2,3,5,250):
            a = self.runner.adapter.new_reader()
            ids = tuple(range(1,count+1))
            snap = DefenseSnapshot('mnt','syntheticaccount',a._clock(),(),frozenset(ids))
            a._snapshot = snap
            parts = a.partition(DefenseDispatch('mnt','syntheticaccount',ids),snap,Event(),workers=4)
            self.assertEqual(len(parts),min(4,count))
            self.assertEqual(tuple(pid for _,d,_ in parts for pid in d.post_ids),ids)
            sizes = [len(d.post_ids) for _,d,_ in parts]
            self.assertLessEqual(max(sizes)-min(sizes),1)
            self.assertIsNone(a._snapshot)

    def test_partial_rejection_counts_other_three_and_stops(self):
        self.server.fault = 'rate_limited'
        self.overlap()
        result = self.runner.run()
        self.assertEqual(result.stopped,'rate_limited')
        self.assertEqual(result.accepted_unverified,15)
        self.assertEqual(result.requests,4)
        self.assertEqual(self.server.active,0)
        self.assertIsNone(self.runner._reader_pool)

    def test_unknown_unapplied_is_not_immediately_replayed(self):
        self.server.fault = 'unknown_unapplied'
        self.overlap()
        result = self.runner.step()
        self.assertEqual(result.accepted_unverified,15)
        self.assertEqual(result.unknown_responses,1)
        self.runner.step()
        self.assertEqual(len(self.server.calls),4)

    def test_user_stop_joins_all_four_before_return(self):
        release = Event()
        self.overlap(release)
        thread = Thread(target=self.runner.run)
        thread.start()
        try:
            self.assertTrue(self.server.post_entered.wait(2))
            # The barrier ensures all four have entered before release.
            for _ in range(100):
                if self.server.active==4:
                    break
                sleep(.01)
            self.assertEqual(self.server.active,4)
            self.runner.request_stop()
            self.assertTrue(thread.is_alive())
        finally:
            self.runner.request_stop()
            release.set()
            thread.join(4)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.server.active,0)
        self.assertEqual(len(self.server.calls),4)

    def test_run_prefetches_while_all_four_are_pending(self):
        barrier = Barrier(4)
        def wait(delay):
            if delay >= .09:
                barrier.wait(timeout=3)
                self.runner._prefetch[1].result(timeout=3)
            else:
                sleep(delay)
        self.server.wait = wait
        result = self.runner.run(lambda _:self.runner.request_stop())
        self.assertEqual(result.accepted_unverified,20)
        self.assertEqual(self.server.max_active,4)
        self.assertGreaterEqual(self.server.gets,4)

    def test_submission_failure_counts_only_started_work(self):
        entered,fail = Event(),Event()
        def wait(delay):
            if delay >= .09:
                entered.set()
                if not fail.wait(3):
                    raise TimeoutError('submit failure not reached')
        self.server.wait = wait
        class FailingPool:
            def __init__(self,**kwargs):
                self.pool,self.count = RealPool(**kwargs),0
            def __enter__(self):
                return self
            def __exit__(self,*args):
                self.pool.shutdown(wait=True)
            def submit(self,*args):
                self.count += 1
                if self.count>=2:
                    fail.set()
                    raise RuntimeError('synthetic resource failure')
                future = self.pool.submit(*args)
                if not entered.wait(3):
                    raise TimeoutError
                return future
        with patch('rectifier.defense_runner.ThreadPoolExecutor',FailingPool):
            result = self.runner.step()
        self.assertEqual(result.stopped,'read_failed')
        self.assertEqual(result.requests,1)
        self.assertEqual(result.accepted_unverified,5)
        self.assertEqual(self.runner.flow.stats.attempted,5)
        self.assertEqual(self.server.active,0)

    def test_four_reversed_results_and_thirteen_part_rejection(self):
        flow = RefreshingDefenseFlow('fixture')
        flow.observe([guest(i) for i in range(1,14)])
        batch = flow.take_batch()
        with self.assertRaises(ValueError):
            flow.record_partitioned_response(batch,tuple(BatchPartResult((i,),BatchResponse.SUCCESS_REPORTED,True) for i in range(1,14)))
        self.assertEqual(flow.stats.accepted_unverified,0)
        flow = RefreshingDefenseFlow('fixture')
        flow.observe([guest(i) for i in range(1,6)])
        batch = flow.take_batch()
        parts = [(1,2),(3,),(4,),(5,)]
        flow.record_partitioned_response(batch,tuple(BatchPartResult(p,BatchResponse.SUCCESS_REPORTED,True) for p in reversed(parts)))
        self.assertEqual(flow.stats.accepted_unverified,5)
