from threading import Event, Thread, Lock
from time import monotonic
from unittest import TestCase
from unittest.mock import Mock, patch

from rectifier.auth import AuthStatus, DEFAULT_RETURN_URL, LoginResult
from rectifier.defense_runner import DefenseRunner, ListDefenseAdapter
from tests.common import ACCOUNT
from tests.common import listing
from tests.common import CONTROL, MENU, response


class DefenseOverlapTests(TestCase):
    def setUp(self):
        denied = patch('socket.socket', side_effect=AssertionError('offline'))
        denied.start()
        self.addCleanup(denied.stop)
        self.pages = [listing((101, 102)) + CONTROL, listing((101, 102, 103)) + CONTROL]
        self.read_count = 0
        self.posts = []
        self.post_hook = lambda: None
        self.read_hook = lambda: None
        self.result = response('{"result":"success"}')
        self.offset = 0
        self.session = Mock()
        self.session.cookie_value.return_value = 'synthetic'
        self.session.request.side_effect = self.request
        login = LoginResult(AuthStatus.VERIFIED, 'synthetic', self.session, ACCOUNT)
        self.clock = lambda: monotonic() + self.offset
        self.runner = DefenseRunner(ListDefenseAdapter(login, 'mnt', clock=self.clock),
                                    after_post_id=1, interval=.25, clock=self.clock)

    def request(self, method, url, **kwargs):
        if method == 'POST':
            self.posts.append(tuple(int(v) for k, v in kwargs['form'] if k == 'nos[]'))
            self.post_hook()
            return self.result
        if url == DEFAULT_RETURN_URL:
            return response(MENU)
        self.read_hook()
        value = self.pages[min(self.read_count, len(self.pages) - 1)]
        self.read_count += 1
        if isinstance(value, Exception):
            raise value
        return response(value) if isinstance(value, str) else value

    def await_prefetch(self):
        # Called inside an uncompleted POST: this cannot succeed in the old loop.
        return self.runner._prefetch[1].result(timeout=3)

    def test_reads_during_post_and_next_batch_excludes_inflight_ids(self):
        self.post_hook = self.await_prefetch
        result = self.runner.run(lambda s: self.runner.request_stop() if s.requests == 2 else None)
        self.assertEqual(self.posts, [(101, 102), (103,)])
        self.assertEqual(result.accepted_unverified, 3)
        self.assertEqual(result.stopped, 'user_stopped')
        self.assertIsNone(self.runner._prefetch)
        self.assertIsNone(self.runner._reader_pool)

    def test_aged_prefetch_is_replaced_by_new_list_not_retimestamped(self):
        self.pages.append(listing((104,)) + CONTROL)
        def slow_post():
            self.await_prefetch()
            if len(self.posts) == 1:
                self.offset += 10
        self.post_hook = slow_post
        self.runner.run(lambda s: self.runner.request_stop() if s.requests == 2 else None)
        self.assertEqual(self.posts, [(101, 102), (104,)])

    def test_member_changed_in_prefetch_is_not_sent(self):
        self.pages[1] = listing((103,), writer='data-uid="member"') + CONTROL
        self.post_hook = self.await_prefetch
        self.runner.run(lambda s: self.runner.request_stop() if s.scans == 2 else None)
        self.assertEqual(self.posts, [(101, 102)])

    def test_failed_prefetch_stops_before_second_post(self):
        self.pages[1] = response('limited', status=429)
        # Don't unwrap the expected exception inside POST: the runner consumes it.
        def wait_read():
            try:
                self.await_prefetch()
            except ValueError:
                pass
        self.post_hook = wait_read
        result = self.runner.run()
        self.assertEqual(result.stopped, 'rate_limited')
        self.assertEqual(self.posts, [(101, 102)])

    def test_rejection_drains_read_and_sends_no_next_post(self):
        self.post_hook = self.await_prefetch
        self.result = response('{"result":"fail"}')
        result = self.runner.run()
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(result.stopped, 'rejection_reported')
        self.assertIsNone(self.runner._reader_pool)

    def test_stop_waits_for_reader_before_returning_session(self):
        in_read, release, finished = Event(), Event(), Event()
        def blocked_read():
            if self.read_count:
                in_read.set()
                if not release.wait(3):
                    raise RuntimeError('test release timeout')
        self.read_hook = blocked_read
        def post():
            self.assertTrue(in_read.wait(3))
            self.runner.request_stop()
        self.post_hook = post
        def run():
            try:
                self.runner.run()
            finally:
                finished.set()
        thread = Thread(target=run)
        thread.start()
        try:
            self.assertTrue(in_read.wait(3))
            self.assertFalse(finished.wait(.1))
        finally:
            release.set()
            thread.join(4)
        self.assertTrue(finished.is_set())
        self.assertEqual(len(self.posts), 1)

    def test_single_owner_and_callback_error_still_join_reader(self):
        def post():
            self.await_prefetch()
            with self.assertRaises(RuntimeError):
                self.runner.step()
        self.post_hook = post
        with self.assertRaisesRegex(RuntimeError, 'callback'):
            self.runner.run(lambda _: (_ for _ in ()).throw(RuntimeError('callback')))
        self.assertTrue(self.runner.cancel.is_set())
        self.assertIsNone(self.runner._reader_pool)
        self.assertEqual(len(self.posts), 1)

    def test_prefetch_is_bounded_while_post_is_blocked(self):
        def post():
            self.await_prefetch()
            count = self.read_count
            self.assertFalse(Event().wait(.3))
            self.assertEqual(self.read_count, count)
        self.post_hook = post
        self.runner.run(lambda _: self.runner.request_stop())
        self.assertEqual(self.read_count, 2)

    def test_empty_prefetch_fails_closed(self):
        self.pages[1] = ''
        def wait_read():
            try:
                self.await_prefetch()
            except ValueError:
                pass
        self.post_hook = wait_read
        result = self.runner.run()
        self.assertEqual(result.stopped, 'empty_response')
        self.assertEqual(len(self.posts), 1)
