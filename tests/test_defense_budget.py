from bisect import bisect_left
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock, Timer
from time import monotonic, sleep
from unittest import TestCase
from unittest.mock import Mock, patch

from rectifier.auth import AuthStatus, LoginResult
from rectifier.batch_journal import BatchResponse
from rectifier.defense_flow import BatchPartResult, RefreshingDefenseFlow
from rectifier.defense_runner import DefenseDispatch, DefenseRunner, ListDefenseAdapter
from rectifier.defense_task import DefenseTask
from rectifier.request_budget import BudgetWaitError, RequestBudget
from tests.defense_simulator import SyntheticBoard
from tests.test_defense_flow import guest


class BudgetTests(TestCase):
    def test_invalid_limits_and_deadlines(self):
        for limit in (0,101,True,1.5,None):
            with self.assertRaises(ValueError):
                RequestBudget(limit)
        for deadline in (True,float('nan'),float('inf'),'later'):
            gate = RequestBudget()
            with self.assertRaises(ValueError):
                gate.acquire(Event(),deadline=deadline)
            self.assertEqual(len(gate._recent),0)

    def test_rolling_window_across_second_boundary_and_bounded_memory(self):
        now, starts = [.999], []
        gate = RequestBudget(2,clock=lambda:now[0],wait=lambda t:now.__setitem__(0,now[0]+t))
        for _ in range(100):
            gate.acquire(Event())
            starts.append(now[0])
            now[0] += .002
            self.assertLessEqual(len(gate._recent),2)
        self.assertLessEqual(max(bisect_left(starts,t+1)-i for i,t in enumerate(starts)),2)

    def test_concurrent_requests_share_one_limit(self):
        gate, starts, lock = RequestBudget(10), [], Lock()
        def work():
            gate.acquire(Event())
            with lock:
                starts.append(monotonic())
        with ThreadPoolExecutor(max_workers=12) as pool:
            futures = [pool.submit(work) for _ in range(20)]
            for future in futures:
                future.result(timeout=3)
        starts.sort()
        self.assertLessEqual(max(bisect_left(starts,t+1)-i for i,t in enumerate(starts)),10)

    def test_cancelled_wait_does_not_consume_slot(self):
        cancel, gate = Event(), RequestBudget(1)
        gate.acquire(cancel)
        timer = Timer(.03,cancel.set)
        timer.start()
        started = monotonic()
        try:
            with self.assertRaisesRegex(BudgetWaitError,'user_stopped'):
                gate.acquire(cancel)
        finally:
            timer.cancel()
            timer.join()
        self.assertLess(monotonic()-started,.3)
        self.assertEqual(len(gate._recent),1)


class ProductDefenseBudgetTests(TestCase):
    def setUp(self):
        blocked = patch('socket.socket',side_effect=AssertionError('offline only'))
        blocked.start()
        self.addCleanup(blocked.stop)

    def make(self, count=60, *, limit=30, fault=None, base=.03):
        server = SyntheticBoard(rate=0,duration=0,initial=count,pages=3,
            list_delay=.001,account_delay=.001,per_post_delay=0,base_delete_delay=base,
            ghost_seconds=0,fault=fault)
        login = LoginResult(AuthStatus.VERIFIED,'synthetic',server,'syntheticaccount')
        adapter = ListDefenseAdapter(login,'mnt',pages=(1,2,3),request_budget=RequestBudget(limit))
        runner = DefenseRunner(adapter,after_post_id=1,delete_workers=12,posts_per_request=5)
        return server,adapter,runner,login

    def barrier(self,server):
        ready = Barrier(12)
        def wait(delay):
            if delay >= .02:
                ready.wait(timeout=3)
                sleep(delay)
            else:
                sleep(delay)
        server.wait = wait

    def test_twelve_posts_overlap_and_cover_sixty_ids_once(self):
        server,adapter,runner,_ = self.make()
        self.barrier(server)
        stats = runner.step()
        self.assertEqual((stats.requests,stats.accepted_unverified),(12,60))
        self.assertEqual(server.max_active,12)
        self.assertEqual([len(c['ids']) for c in server.calls],[5]*12)
        self.assertEqual(len({p for c in server.calls for p in c['ids']}),60)
        self.assertEqual((server.unsafe,server.duplicates,server.active),(0,0,0))
        self.assertIs(adapter.request_budget,adapter.new_reader().request_budget)

    def test_small_batches_and_next_wave_do_not_drop_or_duplicate(self):
        for count in (1,2,5,6,59,60,61):
            with self.subTest(count=count):
                server,_,runner,_ = self.make(count)
                runner.step()
                if count>60:
                    runner.step()
                self.assertEqual(len(server.deleted),count)
                self.assertTrue(all(len(c['ids'])<=5 for c in server.calls))
                self.assertEqual(len(server.calls),(count+4)//5)
                self.assertEqual((server.unsafe,server.duplicates,server.already_deleted),(0,0,0))

    def test_partition_cap_must_cover_all_ids_and_never_silently_truncate(self):
        _,adapter,runner,_ = self.make(61)
        snap = adapter.read_snapshot(runner.cancel)
        dispatch = DefenseDispatch('mnt',adapter.account,tuple(snap.candidates))
        with self.assertRaisesRegex(ValueError,'scope_mismatch'):
            adapter.partition(dispatch,snap,runner.cancel,workers=12,max_part_size=5)
        self.assertIsNone(adapter._snapshot)

    def test_bad_batch_configuration_fails_before_requests(self):
        server,adapter,_,_ = self.make()
        for cap in (0,251,True,1.2):
            with self.assertRaises(ValueError):
                DefenseRunner(adapter,after_post_id=1,delete_workers=12,posts_per_request=cap)
        self.assertEqual(server.gets,0)
        self.assertEqual(server.calls,[])

    def test_twelve_responses_allow_out_of_order_and_partial_unknown(self):
        server,_,runner,_ = self.make(fault='unknown_unapplied')
        self.barrier(server)
        stats = runner.step()
        self.assertEqual((stats.accepted_unverified,stats.unknown_responses),(55,1))
        runner.step()
        self.assertEqual(len(server.calls),12)
        flow = RefreshingDefenseFlow('fixture',batch_size=60)
        flow.observe([guest(i) for i in range(1,61)])
        batch = flow.take_batch()
        parts = [BatchPartResult(tuple(range(i,i+5)),BatchResponse.SUCCESS_REPORTED,True)
                 for i in range(1,61,5)]
        flow.record_partitioned_response(batch,tuple(reversed(parts)))
        self.assertEqual(flow.stats.accepted_unverified,60)

    def test_partial_rejection_collects_all_started_siblings(self):
        server,_,runner,_ = self.make(fault='rate_limited')
        self.barrier(server)
        stats = runner.step()
        self.assertEqual((stats.stopped,stats.accepted_unverified),('rate_limited',55))
        self.assertEqual(server.active,0)
        runner.step()
        self.assertEqual(len(server.calls),12)

    def test_shared_budget_counts_account_list_and_post_and_stops_waiters(self):
        server,_,runner,_ = self.make(limit=5,fault='rate_limited')
        started = monotonic()
        stats = runner.run()
        self.assertEqual(stats.stopped,'rate_limited')
        self.assertEqual((server.gets,len(server.calls)),(3,1))
        self.assertEqual((stats.requests,stats.accepted_unverified),(1,0))
        self.assertEqual(runner.flow.stats.attempted,5)
        self.assertEqual(server.active,0)
        self.assertLess(monotonic()-started,.5)

    def test_user_stop_during_budget_wait_joins_already_started_post(self):
        server,_,runner,_ = self.make(limit=5,base=.12)
        timer = Timer(.04,runner.request_stop)
        timer.start()
        try:
            stats = runner.run()
        finally:
            timer.cancel()
            timer.join()
        self.assertEqual((stats.stopped,stats.requests),('user_stopped',1))
        self.assertEqual(server.active,0)
        self.assertEqual(runner.flow.stats.attempted,5)
        self.assertIsNone(runner._reader_pool)

    def test_expired_list_during_wait_never_sends_or_counts_attempt(self):
        server,adapter,runner,_ = self.make(count=1)
        adapter.max_age = .1
        snapshot = adapter.read_snapshot(runner.cancel)
        gate = RequestBudget(1)
        gate.acquire(Event())
        adapter.request_budget = gate
        with patch.object(runner,'_read_current',return_value=snapshot):
            stats = runner.step()
        self.assertEqual(stats.stopped,'stale_listing')
        self.assertEqual((stats.requests,stats.unknown_responses,runner.flow.stats.attempted),(0,0,0))
        self.assertEqual(server.calls,[])

    def test_ui_task_uses_product_configuration_without_relogin(self):
        _,_,_,login = self.make()
        fake, reader, login_fn = Mock(),Mock(),Mock()
        fake.run.return_value.stopped = 'user_stopped'
        task = DefenseTask('mnt',(1,2,3),login_result=login,password_reader=reader,login_fn=login_fn)
        with patch('rectifier.defense_task.DefenseRunner',return_value=fake) as constructor:
            task._work()
        args,kwargs = constructor.call_args
        self.assertEqual((kwargs['delete_workers'],kwargs['posts_per_request']),(12,5))
        self.assertEqual(args[0].request_budget.limit,30)
        self.assertIs(args[0].request_budget,args[0].new_reader().request_budget)
        reader.assert_not_called()
        login_fn.assert_not_called()

    def test_default_adapter_and_runner_keep_unlimited_single_batch_contract(self):
        server,_,_,login = self.make(count=37)
        adapter = ListDefenseAdapter(login,'mnt')
        self.assertIsNone(adapter.request_budget)
        stats = DefenseRunner(adapter,after_post_id=1).step()
        self.assertEqual((stats.requests,stats.accepted_unverified),(1,37))
        self.assertEqual(len(server.calls[0]['ids']),37)
