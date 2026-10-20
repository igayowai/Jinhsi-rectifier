"""Finite selected-only manual batches; fake HTTP and no credential access."""
import gc
import json
from concurrent.futures import ThreadPoolExecutor as RealPool
from dataclasses import replace
from threading import Barrier, Event, Lock, Thread
from time import monotonic, sleep
from unittest import TestCase
from unittest.mock import Mock, patch

from rectifier.auth import AuthStatus, DEFAULT_RETURN_URL, LoginResult
from rectifier.batch_journal import BatchResponse
from rectifier.defense_runner import DefenseDispatch, DefenseReadError, ListDefenseAdapter
from rectifier.manual_action import ManualAction, ManualReceipt, ManualPart, format_manual_result
from rectifier.request_budget import RequestBudget
from tests.test_account_manual import ACCOUNT, review
from tests.common import listing
from tests.common import CONTROL, MENU, response


class ManualParallelTests(TestCase):
    def setUp(self):
        gc.collect()
        blocked = patch('socket.socket', side_effect=AssertionError('offline only'))
        blocked.start()
        self.addCleanup(blocked.stop)
        self.cancel = Event()
        self.lock = Lock()
        self.sent, self.starts = [], []
        self.active = self.peak = 0
        self.hook = lambda ids: response('{"result":"success"}')
        self.rows = ''
        self.session = Mock()
        self.session.cookie_value.return_value = 'PRIVATE_DUMMY_COOKIE'
        self.session.request.side_effect = self.request
        self.login = LoginResult(AuthStatus.VERIFIED, 'synthetic', self.session, ACCOUNT)

    def request(self, method, url, **kwargs):
        with self.lock:
            self.starts.append(monotonic())
        if method == 'GET':
            return response(MENU.replace('syntheticadmin',ACCOUNT) if url == DEFAULT_RETURN_URL else self.rows)
        ids = tuple(int(v) for k,v in kwargs['form'] if k=='nos[]')
        with self.lock:
            self.sent.append(ids)
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            return self.hook(ids)
        finally:
            with self.lock:
                self.active -= 1

    def action(self, count=37, *, members=False):
        ids = tuple(range(101,101+count))
        writer = 'data-uid="testmember" data-ip=""' if members else 'data-uid="" data-ip="127.0"'
        self.rows = listing(ids+(999,),writer=writer)+CONTROL
        action = ManualAction(self.login, review(ids,'protected_member' if members else 'delete_guest'))
        return action

    def test_37_selected_only_eight_requests_really_overlap_and_log_counts(self):
        action = self.action()
        barrier = Barrier(8)
        def send(ids):
            barrier.wait(2)
            sleep(.02)
            return response('{"result":"success"}')
        self.hook = send
        result = action.execute(self.cancel)
        self.assertEqual(result.result_counts, dict(accepted=37,rejected=0,unknown=0,not_sent=0))
        self.assertEqual((len(self.sent),self.peak,self.active),(8,8,0))
        self.assertTrue(all(len(ids)<=5 for ids in self.sent))
        flat = [pid for ids in self.sent for pid in ids]
        self.assertEqual(len(set(flat)),37)
        self.assertNotIn(999,flat)
        log = action.performance.summary()
        self.assertEqual(log['request_count'],10)
        self.assertEqual(log['result_counts'],result.result_counts)
        self.assertGreater(log['delete_wall_ms'],0)
        self.assertGreater(sum(e['ms'] for e in log['stages'] if e['stage']=='delete'),log['delete_wall_ms'])
        self.assertNotIn('PRIVATE',json.dumps(log))
        self.assertNotIn('post_ids',json.dumps(log))

    def test_250_ids_complete_no_truncation_and_shared_request_budget(self):
        action = self.action(250)
        result = action.execute(self.cancel)
        flat = [pid for ids in self.sent for pid in ids]
        self.assertEqual(set(flat),set(range(101,351)))
        self.assertEqual((len(flat),len(self.sent)),(250,50))
        self.assertEqual(result.result_counts['accepted'],250)
        self.assertLessEqual(self.peak,12)
        self.assertEqual(action.performance.summary()['request_count'],52)
        self.assertEqual(len([e for e in action.performance.events if e['stage']=='delete']),50)
        starts=sorted(self.starts)
        self.assertLessEqual(max(sum(t<=other<t+1 for other in starts) for t in starts),30)

    def test_one_and_five_remain_single_requests(self):
        for count in (1,5):
            self.sent.clear()
            result=self.action(count).execute(self.cancel)
            self.assertEqual(len(self.sent),1)
            self.assertEqual(result.result_counts['accepted'],count)

    def test_explicit_member_selection_remains_scoped_and_defense_rejects_manual(self):
        action = self.action(12,members=True)
        result = action.execute(self.cancel)
        self.assertEqual(result.result_counts['accepted'],12)
        self.assertNotIn(999,[pid for ids in self.sent for pid in ids])
        adapter = ListDefenseAdapter(self.login,'mnt')
        snap=adapter.read_snapshot(self.cancel)
        self.assertFalse(snap.candidates)
        with self.assertRaises(DefenseReadError):
            adapter.partition(DefenseDispatch('mnt',ACCOUNT,(101,102)),snap,self.cancel,manual_selected=True)

    def test_default_partition_still_rejects_manual_policy(self):
        action=self.action(6)
        snap=action.adapter.read_snapshot(self.cancel)
        with self.assertRaises(DefenseReadError):
            action.adapter.partition(DefenseDispatch('mnt',ACCOUNT,(101,102)),snap,self.cancel)
        self.assertFalse(self.sent)

    def test_changed_member_or_title_in_one_selection_blocks_all_before_send(self):
        action=self.action(37)
        self.rows=self.rows.replace('synthetic old post','changed',1)
        with self.assertRaisesRegex(DefenseReadError,'selection_changed'):
            action.execute(self.cancel)
        self.assertEqual(self.sent,[])

    def test_partial_unknown_stops_queued_and_keeps_late_successes(self):
        action=self.action(100)
        barrier=Barrier(12)
        def send(ids):
            barrier.wait(3)
            if 101 in ids:
                raise TimeoutError('PRIVATE_ERROR')
            sleep(.08)
            return response('{"result":"success"}')
        self.hook=send
        result=action.execute(self.cancel)
        c=result.result_counts
        self.assertEqual(sum(c.values()),100)
        self.assertEqual((c['accepted'],c['unknown'],c['not_sent']),(55,5,40))
        self.assertEqual((len(self.sent),self.active),(12,0))
        self.assertEqual(result.stopped,'response_unknown')
        self.assertNotIn('PRIVATE',json.dumps(action.performance.summary()))
        with self.assertRaisesRegex(DefenseReadError,'already_used'):
            action.execute(self.cancel)
        self.assertEqual(len(self.sent),12)

    def test_rate_limit_stops_waiters_without_calling_them_failed(self):
        action=self.action(80)
        action.adapter.request_budget=RequestBudget(3)  # account + list + one POST
        self.hook=lambda ids: response('denied',429)
        result=action.execute(self.cancel)
        self.assertEqual(result.stopped,'rate_limited')
        self.assertEqual(len(self.sent),1)
        c=result.result_counts
        self.assertEqual((c['rejected'],c['unknown'],c['not_sent']),(5,0,75))

    def test_cancel_waits_for_started_call_and_marks_remaining_unsent(self):
        action=self.action(80)
        action.adapter.request_budget=RequestBudget(3)
        entered,release,done=Event(),Event(),Event()
        def send(ids):
            entered.set()
            release.wait(2)
            return response('{"result":"success"}')
        self.hook=send
        results=[]
        def work():
            results.append(action.execute(self.cancel))
            done.set()
        worker=Thread(target=work)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            self.cancel.set()
            self.assertFalse(done.wait(.03))
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        c=results[0].result_counts
        self.assertEqual((c['accepted'],c['not_sent'],c['unknown']),(5,75,0))
        self.assertEqual(results[0].stopped,'user_stopped')

    def test_submission_failure_drains_started_results_and_retains_coverage(self):
        action=self.action(37)
        release=Event()
        def send(ids):
            release.wait(2)
            return response('{"result":"success"}')
        self.hook=send
        class FailPool:
            def __init__(self,**kwargs):
                self.pool=RealPool(**kwargs)
                self.n=0
            def __enter__(self):
                return self
            def __exit__(self,*args):
                self.pool.shutdown(wait=True)
            def submit(self,*args):
                self.n+=1
                if self.n==3:
                    release.set()
                    raise RuntimeError('PRIVATE_THREAD_FAILURE')
                return self.pool.submit(*args)
        with patch('rectifier.manual_action.ThreadPoolExecutor',FailPool):
            result=action.execute(self.cancel)
        self.assertEqual(result.stopped,'read_failed')
        self.assertEqual(sum(result.result_counts.values()),37)
        self.assertEqual(result.result_counts['accepted'],sum(map(len,self.sent)))
        self.assertEqual((result.result_counts['unknown'],self.active),(0,0))

    def test_expired_waiting_child_is_unsent_and_not_retried(self):
        action=self.action(6)
        action.adapter.max_age=.05
        action.adapter.request_budget=RequestBudget(2)
        result=action.execute(self.cancel)
        self.assertEqual(result.stopped,'stale_listing')
        self.assertEqual(result.result_counts,dict(accepted=0,rejected=0,unknown=0,not_sent=6))
        self.assertEqual(self.sent,[])

    def test_result_text_and_ui_preserve_all_four_categories(self):
        from rectifier.defense_runner import DispatchReceipt
        from tests.test_manager_ui import ManagerUITests
        parts=tuple(ManualPart((101+i,),DispatchReceipt(code,attempted)) for i,(code,attempted) in enumerate((
            (BatchResponse.SUCCESS_REPORTED,True),(BatchResponse.REJECTION_REPORTED,True),
            (BatchResponse.UNKNOWN,True),(BatchResponse.UNKNOWN,False))))
        result=ManualReceipt(BatchResponse.UNKNOWN,True,'rate_limited',parts)
        text=format_manual_result(result)
        self.assertIn('수락 1개 · 거부 1개 · 미확인 1개 · 미전송 1개',text)
        self.assertIn('미확인 글 번호: 103',text)
        fixture=ManagerUITests()
        fixture.setUp()
        try:
            fixture.ui.delete_review=review((101,102,103,104))
            with patch.object(fixture.ui,'_delete_dialog') as dialog:
                fixture.ui.delete_messages.put(('manual',(result,None,None)))
                fixture.ui._poll_delete()
            self.assertIn('미전송 1개',dialog.call_args.args[1])
            self.assertIn('서버가 요청을 잠시 제한함',dialog.call_args.args[1])
        finally:
            fixture.tearDown()
