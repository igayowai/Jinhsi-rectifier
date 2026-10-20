"""Offline monitor accuracy, bounded storage, worker isolation and Tk layout."""
import json
import gc
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch
import tkinter as tk

from rectifier.defense_monitor import DefenseMonitor, format_monitor
from rectifier.defense_runner import DefenseRunStats, DefenseRunner, ListDefenseAdapter
from rectifier.defense_task import TaskState
from rectifier.defense_ui import DefenseWindow
from rectifier.auth import AuthStatus, LoginResult
from rectifier.performance import write_summary
from tests.defense_simulator import SyntheticBoard


class Clock:
    now = 100.0
    def __call__(self):
        return self.now


def stats(**kwargs):
    return replace(DefenseRunStats(0, 0, 0, 0, 0, None, 0), **kwargs)


class DefenseMonitorTests(TestCase):
    def setUp(self):
        gc.collect()  # Dispose prior Tk fixtures on their owning main thread.
        block = patch('socket.socket', side_effect=AssertionError('offline only'))
        block.start()
        self.addCleanup(block.stop)
        writer = patch('rectifier.defense_monitor.write_summary', return_value=True)
        self.writer = writer.start()
        self.addCleanup(writer.stop)
        self.clock = Clock()
        self.monitor = DefenseMonitor(clock=self.clock)

    def tearDown(self):
        gc.collect()

    def test_constant_rate_and_idle_decay_use_elapsed_time(self):
        self.assertIsNone(self.monitor.sample(stats())['accepted_per_second'])
        for i in range(1, 61):
            self.clock.now = 100+i*.5
            result = self.monitor.sample(stats(accepted_unverified=i*50, requests=i*10), self.clock.now)
        self.assertEqual(result['accepted_per_second'], 100)
        self.assertEqual(result['delete_requests_per_second'], 20)
        for i in range(1, 25):
            self.clock.now = 130+i*.5
            result = self.monitor.sample(stats(accepted_unverified=3000, requests=600), 130)
        self.assertEqual(result['accepted_per_second'], 0)
        self.assertEqual(result['update_age_seconds'], 12)
        self.assertIn('마지막 집계 12.0초 전', format_monitor(result))

    def test_long_ui_pause_reports_actual_window_not_ten_seconds(self):
        self.clock.now = 130
        result = self.monitor.sample(stats(accepted_unverified=90), 100)
        self.assertEqual((result['window_seconds'], result['accepted_per_second']), (30, 3))
        self.assertIn('최근 30.0초', format_monitor(result))

    def test_high_frequency_polling_has_bounded_memory(self):
        for i in range(10000):
            self.clock.now = 100+i*.08
            self.monitor.sample(stats(accepted_unverified=i))
        self.assertLessEqual(len(self.monitor.history), 42)
        self.assertIsNone(self.monitor.summary['update_age_seconds'])

    def test_five_second_summary_and_final_once_freeze(self):
        writer = Mock(return_value=True)
        for i in range(61):
            self.clock.now = 100+i*.1
            self.monitor.sample(stats(accepted_unverified=i))
            self.monitor.save_if_due(writer=writer)
        writer.assert_called_once()
        self.assertFalse(writer.call_args.args[0]['finished'])
        self.clock.now = 106.1
        result = self.monitor.sample(stats(accepted_unverified=60), 106.1,
            finished=True, reason='rate_limited')
        self.monitor.save_if_due(writer=writer)
        self.clock.now = 200
        self.assertIs(self.monitor.sample(stats(), finished=True), result)
        self.monitor.save_if_due(writer=writer)
        self.assertEqual(writer.call_count, 2)
        self.assertEqual(writer.call_args.args[0]['stop_reason'], 'rate_limited')
        self.assertTrue(writer.call_args.args[0]['finished'])

    def test_log_failure_does_not_escape_or_retry_every_poll(self):
        writer = Mock(side_effect=OSError('PRIVATE_PATH'))
        self.clock.now = 105
        self.monitor.sample(stats())
        self.monitor.save_if_due(writer=writer)
        self.clock.now = 110
        self.monitor.sample(stats(accepted_unverified=500), finished=True)
        self.monitor.save_if_due(writer=writer)
        writer.assert_called_once()
        self.assertTrue(self.monitor.log_failed)
        self.assertEqual(self.monitor.summary['accepted_unverified'], 500)
        self.assertNotIn('PRIVATE_PATH', self.monitor.log_status)

    def test_scalar_whitelist_no_raw_reason_or_attached_data(self):
        data = stats(unknown_responses=3, last_candidates=80, last_deferred=20)
        object.__setattr__(data, 'session', 'PRIVATE_SESSION')
        object.__setattr__(data, 'ids', [999])
        result = self.monitor.sample(data, finished=True, reason='PRIVATE_EXCEPTION', stage='PRIVATE_STAGE')
        self.assertEqual(result['stop_reason'], 'other')
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertNotIn('ids', result)
        self.assertNotIn('confirmed_deleted', result)
        self.assertIn('응답 불명 3회', format_monitor(result))
        self.assertIn('묶음 미배정 20개', format_monitor(result))

    def test_existing_rotating_writer_bounds_monitor_records(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)/'diagnostics.jsonl'
            for i in range(1, 31):
                self.clock.now = 100+i*5
                self.monitor.sample(stats(accepted_unverified=i*500))
                self.monitor.save_if_due(writer=lambda s: write_summary(s, path=path, max_bytes=2000))
            files = list(Path(directory).iterdir())
            self.assertEqual(len(files), 3)
            self.assertTrue(all(p.stat().st_size <= 2000 for p in files))
            saved = [json.loads(line) for p in files for line in p.read_text().splitlines()]
            self.assertEqual(max(x['accepted_unverified'] for x in saved), 15000)
            self.assertTrue(all(x['kind']=='defense' for x in saved))

    def test_runner_captures_unassigned_candidates_before_queue_clear(self):
        server = SyntheticBoard(rate=0, duration=0, initial=80, pages=3,
            list_delay=.001, account_delay=.001, per_post_delay=0,
            base_delete_delay=.001, ghost_seconds=0)
        login = LoginResult(AuthStatus.VERIFIED, 'synthetic', server, 'syntheticaccount')
        runner = DefenseRunner(ListDefenseAdapter(login, 'mnt', pages=(1,2,3)),
                               after_post_id=1, delete_workers=12, posts_per_request=5)
        with patch('rectifier.defense_monitor.write_summary', side_effect=AssertionError('worker must not log')):
            first = runner.step()
            second = runner.step()
        self.assertEqual((first.last_candidates, first.last_deferred, first.queued), (80,20,0))
        self.assertEqual((second.last_candidates, second.last_deferred), (20,0))
        self.assertEqual(second.accepted_unverified, 80)
        self.assertEqual(server.summary()['protected_targets'], 0)

    def test_ui_polls_metrics_and_logs_final_without_network(self):
        from tests.test_defense_ui import FakeTask
        root = tk.Tk()
        root.withdraw()
        window = DefenseWindow(root, 'mnt', (1,), lambda: None, task_factory=FakeTask)
        try:
            window.start()
            window.monitor = self.monitor
            task = window.task
            task.state = TaskState('running', stats(accepted_unverified=50, last_candidates=80, last_deferred=20),
                                   None, 100)
            with patch('rectifier.defense_monitor.write_summary', return_value=True) as writer:
                self.clock.now = 105
                window.window.after_cancel(window.poll_id)
                window._poll()
                self.assertIn('수락 10.0건/초', window.counts.get())
                self.assertIn('묶음 미배정 20개', window.counts.get())
                writer.assert_called_once()
                task.finish()
                window.window.after_cancel(window.poll_id)
                window._poll()
                self.assertEqual(writer.call_count, 2)
                self.assertIn('종료 요약 기록됨', window.log_status.get())
                self.assertIn('사용자 중지', window.status.get())
        finally:
            window.task.finish()
            window.close()
            root.destroy()

    def test_monitor_labels_fit_at_multiple_scales(self):
        with patch('rectifier.dpi.window_dpi', return_value=None), patch(
                'rectifier.dpi.work_area', return_value=(3840,2080)):
            for percent in (100,125,150,200,225,250):
                with self.subTest(percent=percent):
                    root = tk.Tk()
                    root.geometry('1x1+0+0')
                    root.tk.call('tk','scaling',96*percent/100/72)
                    window = DefenseWindow(root,'mnt',(1,2,3),lambda:None,task_factory=Mock())
                    try:
                        self.clock.now = 110
                        window.counts.set(format_monitor(self.monitor.sample(
                            stats(accepted_unverified=6000,last_candidates=150,last_deferred=90),100)))
                        root.update()
                        frame = window.start_button.master.master
                        children = frame.winfo_children()
                        for child in children:
                            self.assertTrue(child.winfo_ismapped(), f'{child}: req={child.winfo_reqheight()} frame={frame.winfo_height()}')
                            self.assertGreaterEqual(child.winfo_height(), child.winfo_reqheight())
                            self.assertLessEqual(child.winfo_y()+child.winfo_height(), frame.winfo_height())
                    finally:
                        window.close()
                        root.destroy()

    def test_close_before_next_poll_still_writes_final_summary_once(self):
        from tests.test_defense_ui import FakeTask
        root = tk.Tk()
        root.withdraw()
        window = DefenseWindow(root, 'mnt', (1,), lambda: None, task_factory=FakeTask)
        try:
            window.start()
            window.task.finish()
            with patch('rectifier.defense_monitor.write_summary', return_value=True) as writer:
                window.close()
                window.close()
                writer.assert_called_once()
                self.assertTrue(writer.call_args.args[0]['finished'])
                self.assertEqual(writer.call_args.args[0]['accepted_unverified'], 2)
        finally:
            if not window.closed:
                window.task.finish()
                window.close()
            root.destroy()

    def test_start_record_is_on_disk_before_task_creation(self):
        from tests.test_defense_ui import FakeTask
        root = tk.Tk()
        root.withdraw()
        window = None
        try:
            with TemporaryDirectory() as directory:
                path = Path(directory) / 'defense.jsonl'
                def factory(*args, **kwargs):
                    records = [json.loads(line) for line in path.read_text().splitlines()]
                    self.assertEqual(len(records), 1)
                    self.assertEqual(records[0]['stage'], 'starting')
                    self.assertFalse(records[0]['finished'])
                    self.assertEqual(records[0]['delete_requests'], 0)
                    return FakeTask(*args, **kwargs)
                with patch('rectifier.defense_monitor.write_summary',
                           side_effect=lambda value: write_summary(value, path=path)):
                    window = DefenseWindow(root, 'mnt', (1,), lambda: None, task_factory=factory)
                    self.assertFalse(path.exists())  # Merely opening remains inert.
                    window.start()
                    self.assertIsNotNone(window.task)
                    window.task.finish()
                    window.close()
                    records = [json.loads(line) for line in path.read_text().splitlines()]
                    self.assertEqual(len(records), 2)
                    self.assertEqual(records[-1]['stage'], 'finished')
                    self.assertTrue(records[-1]['finished'])
        finally:
            if window is not None and not window.closed:
                if window.task is not None:
                    window.task.finish()
                window.close()
            root.destroy()

    def test_creation_and_thread_start_failure_record_once_and_preserve_error(self):
        from tests.test_defense_ui import FakeTask
        class FailedStartTask(FakeTask):
            def start(self):
                self.state = TaskState('starting')
                raise RuntimeError('PRIVATE_EXCEPTION')
        for factory, reason in (
            (Mock(side_effect=RuntimeError('PRIVATE_EXCEPTION')), 'task_creation_failed'),
            (Mock(side_effect=FailedStartTask), 'task_start_failed'),
        ):
            with self.subTest(reason=reason):
                self.writer.reset_mock()
                root = tk.Tk()
                root.withdraw()
                window = DefenseWindow(root, 'mnt', (1,), lambda: None, task_factory=factory)
                try:
                    window.start()
                    self.assertEqual(self.writer.call_count, 2)
                    final = self.writer.call_args.args[0]
                    self.assertTrue(final['finished'])
                    self.assertEqual(final['stop_reason'], reason)
                    self.assertNotIn('PRIVATE', json.dumps(final))
                    window.window.after_cancel(window.poll_id)
                    window._poll()
                    self.assertIn('시작 실패', window.status.get())
                    self.assertIn('종료 요약 기록됨', window.log_status.get())
                    window.start()  # No implicit or repeated launch after failure.
                    factory.assert_called_once()
                    window.close()
                    self.assertEqual(self.writer.call_count, 2)
                finally:
                    if not window.closed:
                        window.close()
                    root.destroy()

    def test_preparation_without_stats_is_saved_every_five_seconds(self):
        from tests.test_defense_ui import FakeTask
        root = tk.Tk()
        root.withdraw()
        window = DefenseWindow(root, 'mnt', (1,), lambda: None, task_factory=FakeTask)
        try:
            with patch('rectifier.defense_ui.DefenseMonitor', return_value=self.monitor):
                window.start()
            for index, stage in enumerate(('login', 'captcha', 'baseline'), 1):
                window.task.state = TaskState(stage, None, None, self.clock.now)
                self.clock.now = 100 + index * 5
                window.window.after_cancel(window.poll_id)
                window._poll()
                self.assertEqual(self.writer.call_count, index + 1)
                self.assertEqual(self.writer.call_args.args[0]['stage'], stage)
                self.assertEqual(self.writer.call_args.args[0]['delete_requests'], 0)
        finally:
            window.task.finish()
            window.close()
            root.destroy()

    def test_initial_log_failure_does_not_prevent_start_or_retry_on_poll(self):
        from tests.test_defense_ui import FakeTask
        root = tk.Tk()
        root.withdraw()
        window = DefenseWindow(root, 'mnt', (1,), lambda: None, task_factory=FakeTask)
        self.writer.return_value = False
        try:
            window.start()
            self.assertTrue(window.task.alive)
            self.assertIn('기록 실패', window.log_status.get())
            window.task.finish()
            window.close()
            self.writer.assert_called_once()
        finally:
            if not window.closed:
                window.task.finish()
                window.close()
            root.destroy()
