import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest import TestCase
from unittest.mock import Mock, patch

from rectifier.auth import AuthStatus, DEFAULT_RETURN_URL, LoginResult
from rectifier.batch_journal import BatchResponse
from rectifier.manual_action import ManualAction
from rectifier.performance import ManualPerformance, write_summary
from tests.test_account_manual import ACCOUNT, review
from tests.common import listing
from tests.common import CONTROL, MENU, response


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class PerformanceTests(TestCase):
    def setUp(self):
        block = patch('socket.socket', side_effect=AssertionError('offline only'))
        block.start()
        self.addCleanup(block.stop)
        self.clock = Clock()
        self.ids = tuple(range(101, 106))
        self.session = Mock()
        self.session.cookie_value.return_value = 'DO_NOT_LOG_TOKEN'
        self.session.request.side_effect = self.request
        self.result = response('{"result":"success"}')
        self.login = LoginResult(AuthStatus.VERIFIED, 'DO_NOT_LOG_LOGIN', self.session, ACCOUNT)

    def request(self, method, url, **kwargs):
        if method == 'POST':
            self.clock.now += 3
            if isinstance(self.result, Exception):
                raise self.result
            return self.result
        if url == DEFAULT_RETURN_URL:
            self.clock.now += .5
            return response(MENU.replace('syntheticadmin', ACCOUNT))
        self.clock.now += 2
        return response(listing(self.ids) + CONTROL)

    def action(self):
        action = ManualAction(self.login, review(self.ids))
        action.performance.clock = self.clock
        return action

    def test_five_posts_stage_timings_and_ui_wait_no_extra_requests(self):
        action = self.action()
        receipt = action.execute(Event())
        self.assertEqual(receipt.response, BatchResponse.SUCCESS_REPORTED)
        self.clock.now += .2
        action.performance.mark_ui_ready()
        data = action.performance.summary()
        self.assertEqual(data['worker_ms'], 5500)
        self.assertEqual(data['ui_ready_ms'], 5700)
        self.assertEqual(data['ui_after_worker_ms'], 200)
        self.assertEqual(data['selected_count'], 5)
        self.assertNotIn('preserve', data)
        self.assertEqual(data['request_count'], 3)
        self.assertEqual([c.args[0] for c in self.session.request.call_args_list], ['GET', 'GET', 'POST'])
        stages = {e['stage']: e for e in data['stages']}
        self.assertEqual(stages['account']['ms'], 500)
        self.assertEqual(stages['list']['ms'], 2000)
        self.assertEqual(stages['delete']['ms'], 3000)
        self.assertEqual(stages['delete']['http_status'], 200)
        text = json.dumps(data)
        for secret in ('DO_NOT_LOG', 'synthetic old post', 'nos[]', 'ci_t', ACCOUNT, 'https://'):
            self.assertNotIn(secret, text)

    def test_timeout_is_unknown_not_resent_and_raw_error_excluded(self):
        self.result = TimeoutError('DO_NOT_LOG_EXCEPTION')
        action = self.action()
        receipt = action.execute(Event())
        self.assertEqual(receipt.response, BatchResponse.UNKNOWN)
        data = action.performance.summary()
        self.assertTrue(data['attempted'])
        event = next(e for e in data['stages'] if e['stage'] == 'delete')
        self.assertTrue(event['failed'])
        self.assertIsNone(event['http_status'])
        self.assertEqual(data['request_count'], 3)
        self.assertNotIn('DO_NOT_LOG', json.dumps(data))

    def test_http_rejection_records_status_without_changing_stop(self):
        self.result = response('DO_NOT_LOG_REMOTE_MESSAGE', 429)
        action = self.action()
        receipt = action.execute(Event())
        self.assertEqual(receipt.stopped, 'rate_limited')
        data = action.performance.summary()
        self.assertEqual(data['reason'], 'rate_limited')
        self.assertEqual(data['stages'][-1]['http_status'], 429)
        self.assertNotIn('DO_NOT_LOG', json.dumps(data))

    def test_preparation_failure_records_duration_no_network(self):
        def fail_snapshot(*args):
            self.clock.now += 2
            raise OSError('DO_NOT_LOG_PRIVATE_DETAIL')
        action = self.action()
        with patch.object(action.adapter, 'read_snapshot', side_effect=fail_snapshot), self.assertRaises(OSError):
            action.execute(Event())
        data = action.performance.summary()
        self.assertEqual(data['stages'][0]['ms'], 2000)
        self.assertEqual(data['request_count'], 0)
        self.assertEqual(data['reason'], 'preparation_failed')
        self.assertNotIn('DO_NOT_LOG', json.dumps(data))
        self.session.request.assert_not_called()

    def test_cancel_before_send_still_has_summary(self):
        action = self.action()
        cancel = Event()
        cancel.set()
        with self.assertRaises(ValueError):
            action.execute(cancel)
        data = action.performance.summary()
        self.assertEqual(data['reason'], 'user_stopped')
        self.assertEqual(data['result'], 'not_sent')
        self.assertFalse(data['attempted'])

    def test_writer_failure_and_duplicate_save_do_not_repeat_action(self):
        action = self.action()
        receipt = action.execute(Event())
        writer = Mock(side_effect=OSError('DO_NOT_LOG_DISK_ERROR'))
        self.assertFalse(action.performance.save(writer=writer))
        self.assertFalse(action.performance.save(writer=writer))
        writer.assert_called_once()
        self.assertEqual(receipt.response, BatchResponse.SUCCESS_REPORTED)
        self.assertEqual(self.session.request.call_count, 3)

    def test_rotation_bounded_and_oldest_diagnostic_discarded(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'perf.jsonl'
            for number in range(60):
                self.assertTrue(write_summary({'sequence': number, 'ms': 5000}, path=path, max_bytes=160))
            files = list(Path(directory).iterdir())
            self.assertEqual(len(files), 3)
            self.assertTrue(all(p.stat().st_size <= 160 for p in files))
            records = [json.loads(line) for p in files for line in p.read_text().splitlines()]
            self.assertEqual(max(r['sequence'] for r in records), 59)
            self.assertNotIn(0, [r['sequence'] for r in records])

    def test_file_failure_is_quiet_and_import_needs_no_directory(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'no-access' / 'perf.jsonl'
            with patch('pathlib.Path.mkdir', side_effect=PermissionError('DO_NOT_LOG')), patch('sys.stderr') as stderr:
                self.assertFalse(write_summary({'ms': 1}, path=path))
                stderr.write.assert_not_called()
            self.assertTrue(write_summary({'ms': 1}, path=path))
            with patch('rectifier.performance._QuietRotatingHandler.doRollover', side_effect=OSError('DO_NOT_LOG')), patch('sys.stderr') as stderr:
                self.assertFalse(write_summary({'ms': 1}, path=path, max_bytes=1))
                stderr.write.assert_not_called()

    def test_unknown_reason_cannot_inject_secret_into_log(self):
        performance = ManualPerformance(1, 1, clock=self.clock)
        performance.start()
        performance.complete(reason='DO_NOT_LOG_EXCEPTION')
        self.assertEqual(performance.summary()['reason'], 'other')

    def test_ui_completion_reports_duration_and_persists_once(self):
        from tests.test_manager_ui import ManagerUITests
        fixture = ManagerUITests()
        fixture.setUp()
        try:
            performance = ManualPerformance(3, 1, clock=self.clock)
            fixture.action.performance = performance
            receipt = fixture.action.execute.return_value
            def execute(*args, **kwargs):
                self.clock.now += 5
                performance.complete(receipt)
                return receipt
            fixture.action.execute.side_effect = execute
            fixture.authenticate()
            fixture.select()
            fixture.ui.prepare_delete()
            with patch.object(ManualPerformance, 'save', autospec=True, return_value=True) as save:
                fixture.ui.confirm_delete()
                fixture.wait(lambda: fixture.ui.delete_phase is None)
                save.assert_called_once_with(performance)
            self.assertIn('5.00초', fixture.ui.status.get())
            self.assertIn('진단 로그 저장됨', fixture.ui.status.get())
            self.assertEqual(performance.summary()['ui_ready_ms'], 5000)
        finally:
            fixture.tearDown()
