from dataclasses import asdict, replace
from threading import Event
from unittest import TestCase
from unittest.mock import Mock, patch

from rectifier.auth import AuthStatus, DEFAULT_RETURN_URL, LoginResult
from rectifier.defense_runner import DefenseDispatch, DefenseRunner, ListDefenseAdapter
from rectifier.live import DELETE_URL
from tests.common import ACCOUNT
from tests.common import listing
from tests.common import CONTROL, MENU, response


class DefenseRunnerTests(TestCase):
    def setUp(self):
        for target in ('socket.socket', 'sqlite3.connect'):
            blocked = patch(target, side_effect=AssertionError(f'{target} forbidden'))
            blocked.start()
            self.addCleanup(blocked.stop)
        self.now = 0.0
        self.cancel = Event()
        self.pages = {1: listing((101, 102)) + CONTROL}
        self.menu = MENU
        self.outcome = response('{"result":"success"}')
        self.read_hook = lambda url: None
        self.post_hook = lambda kwargs: None
        self.session = Mock()
        self.session.cookie_value.return_value = 'synthetic-test-token'
        self.session.request.side_effect = self.request
        self.login = LoginResult(AuthStatus.VERIFIED, 'synthetic', self.session, ACCOUNT)

    def request(self, method, url, **kwargs):
        if method == 'POST':
            self.assertEqual(url, DELETE_URL)
            self.post_hook(kwargs)
            result = self.outcome
        else:
            self.read_hook(url)
            if url == DEFAULT_RETURN_URL:
                result = self.menu
            else:
                self.assertIn('/mgallery/board/lists/?id=mnt&page=', url)
                result = self.pages[int(url.rsplit('=', 1)[1])]
            if isinstance(result, str):
                result = response(result)
        if isinstance(result, Exception):
            raise result
        return result

    def adapter(self, **kwargs):
        return ListDefenseAdapter(self.login, 'mnt', clock=lambda: self.now, **kwargs)

    def runner(self, *, pages=(1,), **kwargs):
        return DefenseRunner(self.adapter(pages=pages), after_post_id=kwargs.pop('after_post_id', 1), cancel=self.cancel,
                             clock=lambda: self.now, **kwargs)

    def posts(self):
        return [call for call in self.session.request.call_args_list if call.args[0] == 'POST']

    def sent_ids(self, index=-1):
        return [v for k, v in self.posts()[index].kwargs['form'] if k == 'nos[]']

    def test_boundary_uses_latest_page_and_includes_members(self):
        self.pages[1] = listing((205,), writer='data-uid="member"') + CONTROL
        self.pages[1500] = listing((50, 51)) + CONTROL
        adapter = self.adapter(pages=(1500,))
        boundary = adapter.read_start_boundary(self.cancel)
        self.assertEqual(boundary, 205)
        self.assertEqual(adapter.pages, (1500,))
        self.assertFalse(self.posts())
        self.assertFalse(any('page=1500' in call.args[1] for call in self.session.request.call_args_list))
        runner = self.runner(after_post_id=boundary, pages=(1500,))
        runner.step()
        self.assertFalse(self.posts())
        self.pages[1500] = listing((50, 205, 206)) + CONTROL
        runner.step()
        self.assertEqual(self.sent_ids(), ['206'])

    def test_boundary_protects_old_rows_after_page_shift_and_cooldown(self):
        boundary = self.adapter().read_start_boundary(self.cancel)
        runner = self.runner(after_post_id=boundary, delete_workers=12, posts_per_request=5)
        runner.step()
        self.assertFalse(self.posts())
        self.pages[1] = listing((90, 100, 101, 102, 103, 104)) + CONTROL
        runner.step()
        self.assertEqual({pid for call in self.posts() for key, pid in call.kwargs['form'] if key == 'nos[]'}, {'103', '104'})
        self.now += 10
        self.pages[1] = listing((80, 90, 101, 102)) + CONTROL
        runner.step()
        self.assertEqual(runner.stats.accepted_unverified, 2)

    def test_restart_establishes_new_boundary(self):
        first_boundary = self.adapter().read_start_boundary(self.cancel)
        self.pages[1] = listing((101, 102, 103)) + CONTROL
        self.runner(after_post_id=first_boundary).step()
        count = len(self.posts())
        next_boundary = self.adapter().read_start_boundary(self.cancel)
        runner = self.runner(after_post_id=next_boundary)
        runner.step()
        self.assertEqual(len(self.posts()), count)
        self.pages[1] = listing((101, 103, 104)) + CONTROL
        runner.step()
        self.assertEqual(self.sent_ids(), ['104'])

    def test_missing_or_invalid_boundary_cannot_construct_runner(self):
        with self.assertRaises(TypeError):
            DefenseRunner(self.adapter())
        for value in (None, 0, -1, True, '102'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                DefenseRunner(self.adapter(), after_post_id=value)
        self.assertFalse(self.posts())

    def test_empty_or_failed_baseline_never_posts(self):
        from rectifier.defense_runner import DefenseReadError
        for body in (listing(()) + CONTROL, response('', 200), response('limited', 429)):
            self.pages[1] = body
            with self.subTest(body=str(body)[:20]), self.assertRaises(DefenseReadError):
                self.adapter().read_start_boundary(self.cancel)
        self.assertFalse(self.posts())

    def test_current_guest_batch_and_new_scan_without_db_or_management_reads(self):
        runner = self.runner()
        runner.step()
        self.assertEqual(self.sent_ids(), ['101', '102'])
        self.assertEqual(runner.stats.accepted_unverified, 2)
        self.assertEqual(runner.stats.confirmed_deleted, 0)
        self.pages[1] = listing((101, 102, 103)) + CONTROL
        runner.step()
        self.assertEqual(runner.stats.requests, 2)
        self.assertEqual(runner.stats.accepted_unverified, 3)
        self.assertEqual(self.sent_ids(), ['103'])
        urls = [call.args[1] for call in self.session.request.call_args_list]
        self.assertFalse(any('/board/view/' in url or '/management/' in url for url in urls))
        self.assertNotIn('synthetic-test-token', str(asdict(runner.stats)))

    def test_restart_uses_current_list_and_does_not_load_old_requests(self):
        self.runner().step()
        self.pages[1] = listing((101, 103)) + CONTROL
        runner = self.runner()
        runner.step()
        self.assertEqual(runner.stats.requests, 1)
        self.assertEqual(self.sent_ids(), ['101', '103'])

    def test_member_unknown_recommended_notice_never_posted(self):
        for args in ({'writer': 'data-uid="member"'}, {'writer': 'data-ip="192.0"'},
                     {'kind': 'icon_notice'}, {'kind': 'icon_recomimg'}):
            with self.subTest(args=args):
                self.pages[1] = listing((101, 102), **args) + CONTROL
                self.assertEqual(self.runner().step().requests, 0)

    def test_defense_recommended_guests_reach_partitioned_post_but_old_rows_do_not(self):
        self.pages = {
            1: listing((99, 101), kind='icon_recomtxt') + CONTROL,
            2: listing((102,), kind='icon_recomimg') + CONTROL,
            3: listing((103,)) + CONTROL,
        }
        adapter = self.adapter(pages=(1, 2, 3), include_recommended=True)
        self.assertTrue(adapter.new_reader().include_recommended)
        runner = DefenseRunner(adapter, after_post_id=100, cancel=self.cancel,
                               clock=lambda: self.now, delete_workers=12, posts_per_request=5)
        runner.step()
        ids = [pid for call in self.posts() for key, pid in call.kwargs['form'] if key == 'nos[]']
        self.assertEqual(set(ids), {'101', '102', '103'})
        self.assertEqual(len(ids), 3)
        for call in self.posts():
            part = [pid for key, pid in call.kwargs['form'] if key == 'nos[]']
            if set(part) & {'101', '102'}:
                self.assertEqual(len(part), 1)
        self.assertIsNone(runner.stats.stopped)

    def test_many_recommended_posts_use_at_most_twelve_single_requests(self):
        self.pages[1] = listing(tuple(range(101, 131)), kind='icon_recomimg') + CONTROL
        runner = DefenseRunner(self.adapter(include_recommended=True), after_post_id=100,
            clock=lambda: self.now, delete_workers=12, posts_per_request=5)
        runner.step()
        self.assertEqual(runner.stats.requests, 12)
        self.assertEqual(runner.stats.last_deferred, 18)
        self.assertEqual(runner.stats.accepted_unverified, 12)
        runner.step()  # Fresh observation excludes the first twelve during cooldown.
        self.assertEqual(runner.stats.requests, 24)
        self.assertEqual(runner.stats.accepted_unverified, 24)
        ids = []
        for call in self.posts():
            part = [pid for key, pid in call.kwargs['form'] if key == 'nos[]']
            self.assertEqual(len(part), 1)
            ids.extend(part)
        self.assertEqual(len(set(ids)), 24)

    def test_direct_multi_recommended_send_is_blocked_before_post(self):
        self.pages[1] = listing((101, 102), kind='icon_recomtxt') + CONTROL
        adapter = self.adapter(include_recommended=True)
        snapshot = adapter.read_snapshot(self.cancel)
        result = adapter.send(DefenseDispatch('mnt', ACCOUNT, (101, 102)), snapshot, self.cancel)
        self.assertEqual(result.stopped, 'scope_mismatch')
        self.assertFalse(result.attempted)
        self.assertFalse(self.posts())

    def test_recommended_policy_still_protects_member_unknown_notice_and_conflicts(self):
        for args in ({'kind': 'icon_recomtxt', 'writer': 'data-uid="member"'},
                     {'kind': 'icon_recomimg', 'writer': ''},
                     {'kind': 'icon_notice'}, {'kind': 'unknown'}):
            with self.subTest(args=args):
                self.pages[1] = listing((101,), **args) + CONTROL
                runner = DefenseRunner(self.adapter(include_recommended=True), after_post_id=100)
                self.assertEqual(runner.step().requests, 0)
        self.pages = {1: listing((101,), kind='icon_recomtxt') + CONTROL,
                      2: listing((101,), kind='icon_notice') + CONTROL}
        runner = DefenseRunner(self.adapter(pages=(1, 2), include_recommended=True), after_post_id=100)
        self.assertEqual(runner.step().stopped, 'invalid_listing')
        self.assertFalse(self.posts())

    def test_more_pages_add_sequential_reads_not_deletion_capacity(self):
        for count in (1, 3, 5):
            with self.subTest(count=count):
                self.now = 0.0
                self.session.request.reset_mock()
                self.pages = {p: listing(range(1000 - p * 50, 1050 - p * 50)) + CONTROL
                              for p in range(1, count + 1)}
                def latency(url):
                    self.now += .2
                self.read_hook = latency
                runner = self.runner(pages=tuple(range(1, count + 1)),
                                     delete_workers=12, posts_per_request=5)
                stats = runner.step()
                gets = [c for c in self.session.request.call_args_list if c.args[0] == 'GET']
                self.assertEqual(len(gets), count + 1)
                self.assertAlmostEqual(stats.last_cycle_ms, (count + 1) * 200)
                self.assertEqual(stats.accepted_unverified, min(60, count * 50))
                self.assertIsNone(stats.stopped)

    def test_account_changed_missing_control_and_wrong_gallery_stop_before_post(self):
        cases = [('menu', MENU.replace(ACCOUNT, 'another_account'), 'auth_required'),
                 ('page', listing(), 'forbidden'),
                 ('page', listing().replace('id=mnt', 'id=other') + CONTROL, 'invalid_listing')]
        for target, value, reason in cases:
            with self.subTest(reason=reason):
                self.menu = value if target == 'menu' else MENU
                self.pages[1] = value if target == 'page' else listing() + CONTROL
                self.assertEqual(self.runner().step().stopped, reason)
        self.assertEqual(self.posts(), [])

    def test_all_pages_required_and_notice_conflict_blocks_everything(self):
        for body in (response('unavailable', 503), listing((101,), kind='icon_notice') + CONTROL):
            with self.subTest(body=str(body)[:30]):
                self.pages[2] = body
                self.assertTrue(self.runner(pages=(1, 2)).step().stopped)
        self.assertEqual(self.posts(), [])

    def test_notice_conflict_is_detected_in_either_page_order(self):
        for notice_page in (1, 2):
            with self.subTest(notice_page=notice_page):
                self.pages = {1: listing((101, 102)) + CONTROL, 2: listing((101, 103)) + CONTROL}
                self.pages[notice_page] = listing((101,), kind='icon_notice') + CONTROL
                self.assertEqual(self.runner(pages=(1, 2)).step().stopped, 'invalid_listing')
        self.assertEqual(self.posts(), [])

    def test_excluded_ids_do_not_leak_into_the_next_scan(self):
        self.pages[1] = listing((101,), kind='icon_notice') + CONTROL
        runner = self.runner()
        self.assertEqual(runner.step().requests, 0)
        self.pages[1] = listing((101,)) + CONTROL
        runner.step()
        self.assertEqual(self.sent_ids(), ['101'])

    def test_multipage_duplicate_ids_sent_once(self):
        self.pages[2] = listing((102, 103)) + CONTROL
        self.runner(pages=(1, 2)).step()
        self.assertEqual(self.sent_ids(), ['101', '102', '103'])

    def test_default_batch_covers_selected_pages_in_one_request(self):
        for page in range(1, 6):
            self.pages[page] = listing(range(page*50, (page+1)*50)) + CONTROL
        runner = self.runner(pages=(1, 2, 3, 4, 5))
        runner.step()
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(self.sent_ids(), list(map(str, range(50, 300))))
        self.assertEqual(runner.stats.accepted_unverified, 250)
        runner.step()
        self.assertEqual(len(self.posts()), 1)

    def test_list_shift_discards_unissued_old_candidates(self):
        runner = self.runner(batch_size=1)
        runner.step()
        self.pages[1] = listing((103,)) + CONTROL
        runner.step()
        self.assertEqual(self.sent_ids(), ['103'])

    def test_read_delay_is_included_in_snapshot_age(self):
        def slow(url):
            if '/board/lists/' in url:
                self.now += 4
        self.read_hook = slow
        stats = self.runner().step()
        self.assertEqual(stats.stopped, 'stale_listing')
        self.assertEqual(stats.last_cycle_ms, 4000)
        self.assertEqual(self.posts(), [])

    def test_cookie_delay_expires_snapshot_before_post(self):
        def delayed(*args):
            self.now += 4
            return 'synthetic-test-token'
        self.session.cookie_value.side_effect = delayed
        self.assertEqual(self.runner().step().stopped, 'stale_listing')
        self.assertEqual(self.posts(), [])

    def test_stop_before_during_read_and_cookie_access(self):
        for phase in ('before', 'read', 'cookie'):
            with self.subTest(phase=phase):
                self.cancel.clear()
                self.read_hook = (lambda _: self.cancel.set()) if phase == 'read' else (lambda _: None)
                self.session.cookie_value.side_effect = (
                    lambda *_: self.cancel.set() or 'synthetic-test-token') if phase == 'cookie' else None
                if phase == 'before':
                    self.cancel.set()
                stats = self.runner().step()
                self.assertEqual(stats.stopped, 'user_stopped')
                self.assertEqual(stats.requests, 0)
        self.assertEqual(self.posts(), [])

    def test_stop_during_post_counts_late_ack_and_stops_next_work(self):
        self.post_hook = lambda _: self.cancel.set()
        runner = self.runner()
        runner.step()
        self.now = 10
        stats = runner.step()
        self.assertEqual(stats.stopped, 'user_stopped')
        self.assertEqual(stats.accepted_unverified, 2)
        self.assertEqual(len(self.posts()), 1)

    def test_explicit_rejection_auth_rate_limit_and_http_error_never_retry(self):
        outcomes = [(response('{"result":"fail","msg":"quota"}'), 'rejection_reported'),
                    (response('no', 401), 'auth_required'), (response('no', 403), 'forbidden'),
                    (response('slow', 429), 'rate_limited'), (response('error', 500), 'rejection_reported')]
        for outcome, reason in outcomes:
            with self.subTest(reason=reason):
                self.outcome = outcome
                before = len(self.posts())
                runner = self.runner()
                runner.step()
                self.now += 10
                runner.step()
                self.assertEqual(runner.stats.stopped, reason)
                self.assertEqual(len(self.posts()), before+1)
                self.assertEqual(runner.stats.accepted_unverified, 0)

    def test_unknown_timeout_continues_new_ids_then_rechecks_remaining_ids_after_cooldown(self):
        for outcome in (response('bad'), response('{"result":"success","partial":1}'),
                        TimeoutError('sensitive synthetic text')):
            with self.subTest(outcome=type(outcome).__name__):
                self.pages[1] = listing((101, 102)) + CONTROL
                self.outcome = outcome
                runner = self.runner()
                runner.step()
                self.assertIsNone(runner.stats.stopped)
                self.assertEqual(runner.stats.unknown_responses, 1)
                self.pages[1] = listing((101, 102, 103)) + CONTROL
                self.now += 1
                self.outcome = response('{"result":"success"}')
                runner.step()
                self.assertEqual(self.sent_ids(), ['103'])
                self.pages[1] = listing((102, 103, 104)) + CONTROL
                self.now += 1
                runner.step()
                self.assertEqual(self.sent_ids(), ['102', '104'])
                self.assertEqual(runner.stats.accepted_unverified, 3)
                self.assertNotIn('sensitive', str(runner.stats))

    def test_retry_reclassifies_former_guest_as_member(self):
        runner = self.runner()
        runner.step()
        self.now = 3
        self.pages[1] = listing((101, 102), writer='data-uid="new-member"') + CONTROL
        runner.step()
        self.assertEqual(len(self.posts()), 1)

    def test_read_rate_limit_stops_without_cookie_access(self):
        self.pages[1] = response('slow', 429)
        runner = self.runner()
        runner.step()
        runner.step()
        self.assertEqual(runner.stats.stopped, 'rate_limited')
        self.session.cookie_value.assert_not_called()

    def test_reentrant_step_cannot_overlap_inflight_post(self):
        runner = self.runner()
        def during_post(_):
            with self.assertRaises(RuntimeError):
                runner.step()
        self.post_hook = during_post
        runner.step()
        self.assertEqual(len(self.posts()), 1)

    def test_forged_scope_and_snapshot_reuse_cannot_send(self):
        adapter = self.adapter()
        snapshot = adapter.read_snapshot(self.cancel)
        dispatch = DefenseDispatch('mnt', ACCOUNT, (101, 102))
        receipt = adapter.send(replace(dispatch, post_ids=(999,)), snapshot, self.cancel)
        self.assertFalse(receipt.attempted)
        self.assertEqual(receipt.stopped, 'scope_mismatch')
        self.assertFalse(adapter.send(dispatch, snapshot, self.cancel).attempted)
        snapshot = adapter.read_snapshot(self.cancel)
        self.assertTrue(adapter.send(dispatch, snapshot, self.cancel).attempted)
        self.assertFalse(adapter.send(dispatch, snapshot, self.cancel).attempted)
        self.assertEqual(len(self.posts()), 1)

    def test_run_stop_wakes_wait_and_callback_failure_latches_stop(self):
        runner = self.runner()
        result = runner.run(lambda _: runner.request_stop())
        self.assertEqual(result.stopped, 'user_stopped')
        self.assertEqual(result.requests, 1)
        self.cancel.clear()
        runner = self.runner()
        with self.assertRaises(RuntimeError):
            runner.run(lambda _: (_ for _ in ()).throw(RuntimeError('synthetic callback')))
        self.assertEqual(runner.stats.stopped, 'user_stopped')

    def test_invalid_login_and_options_fail_before_io(self):
        with self.assertRaises(ValueError):
            ListDefenseAdapter(replace(self.login, status=AuthStatus.ACCEPTED_UNVERIFIED), 'mnt')
        for value in (0, float('nan'), 61):
            with self.assertRaises(ValueError):
                self.adapter(max_age=value)
            with self.assertRaises(ValueError):
                self.runner(cooldown=value)
        with self.assertRaises(ValueError):
            self.runner(interval=0)
        self.session.request.assert_not_called()
