"""Offline bump contract and scheduling tests. All network access is denied."""
from dataclasses import replace
from threading import Event, Thread
from unittest import TestCase
from unittest.mock import Mock, patch
import json

from rectifier.auth import AuthStatus, HttpResponse, LoginResult, DEFAULT_RETURN_URL
from rectifier.bump import (BUMP_URL, BumpAdapter, BumpAttempt, BumpLimits, BumpRunner,
                            BumpStopped, BumpTarget, _BumpControls, assess_bump_response)
from rectifier.demo import DEMO_HTML


TARGET = BumpTarget('mnt', 647172)
ACCOUNT = 'synthetic_user'
MENU = '''<header class="header"><ul id="top_depthbox">
<li><a href="https://m.dcinside.com/gallog/synthetic_user">me</a></li>
<li><a href="https://msign.dcinside.com/logout">out</a></li></ul></header>'''
VIEW = '''<input id="gallery_id" value="mnt"><input id="no" value="647172">
<input id="_GALLTYPE_" value="M"><div class="gallview_head">
<span class="title_subject">synthetic notice text</span>
<div class="gall_writer" data-uid="member" data-ip=""></div>
<span class="gall_date">2026.09.16 06:38:21</span><span class="gall_comment">댓글 0</span></div>
<div class="writing_view_box"><div class="write_div">synthetic body</div></div>'''
CONTROL = '<div class="list_bottom_btnbox"><div class="useradmin_btnbox"><button onclick="update_bump()">끌올</button></div></div>'
SUCCESS = '{"result":"success","msg":"끌올되었습니다."}'


def response(body, status=200):
    return HttpResponse(status, {'Content-Type': 'text/html'}, body)


class Clock:
    def __init__(self):
        self.now = 0.

    def __call__(self):
        return self.now


class Cancel:
    def __init__(self, clock):
        self.clock, self.flag, self.on_wait = clock, False, None

    def is_set(self):
        return self.flag

    def set(self):
        self.flag = True

    def wait(self, seconds):
        if self.on_wait:
            self.on_wait()
        if not self.flag:
            self.clock.now += seconds
        return self.flag


class FakeSession:
    def __init__(self, clock):
        self.clock = clock
        self.closed = False
        self.calls = []
        self.posts = []
        self.view, self.menu, self.listing = VIEW, MENU, DEMO_HTML + CONTROL
        self.reply = response(SUCCESS)
        self.latency = .1
        self.before_post = None

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        if method == 'GET':
            return response({DEFAULT_RETURN_URL: self.menu, TARGET.list_url: self.listing, TARGET.url: self.view}[url])
        if self.before_post:
            self.before_post()
        self.posts.append((self.clock(), url, kwargs))
        self.clock.now += self.latency
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply

    def cookie_value(self, name, url):
        return 'synthetic-dummy-token'

    def close(self):
        self.closed = True


class BumpTests(TestCase):
    def setUp(self):
        no_network = patch('socket.socket', side_effect=AssertionError('offline only'))
        no_network.start()
        self.addCleanup(no_network.stop)
        self.clock = Clock()
        self.cancel = Cancel(self.clock)
        self.session = FakeSession(self.clock)
        self.login = LoginResult(AuthStatus.VERIFIED, 'synthetic', self.session, ACCOUNT)

    def run_bumps(self, *, limits=None, progress=lambda stats: None):
        adapter = BumpAdapter(self.login, TARGET, ACCOUNT, clock=self.clock)
        runner = BumpRunner(adapter, limits or BumpLimits(duration=3, max_requests=4),
                            cancel=self.cancel, clock=self.clock)
        result = runner.run(progress)
        return result, runner

    def test_url_is_canonical_fixed_and_rejects_ambiguous_targets(self):
        self.assertEqual(BumpTarget.from_url(TARGET.url + '&page=2'), TARGET)
        for url in (TARGET.url.replace('https:', 'http:'), TARGET.url + '&no=12',
                    TARGET.url + '&id=mnt', TARGET.url.replace('mnt', '../mnt'),
                    TARGET.url.replace('gall.dcinside.com', 'gall.dcinside.com.evil.invalid'),
                    TARGET.url.replace('https://', 'https://user@'), TARGET.url + '#fragment',
                    TARGET.url.replace('no=647172', 'no=0'), TARGET.url + '\n',
                    TARGET.url.replace('/mgallery/', '/mini/')):
            with self.subTest(url=url), self.assertRaises(ValueError):
                BumpTarget.from_url(url)

    def test_limits_reject_unbounded_burst_and_nonfinite_values(self):
        for kwargs in ({'interval': .49}, {'interval': float('nan')}, {'duration': float('inf')},
                       {'duration': 18001}, {'duration': 0}, {'max_requests': 36001},
                       {'jitter': -.1}, {'jitter': .21}, {'jitter': float('nan')},
                       {'max_requests': True}, {'max_requests': 0}, {'interval': True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                BumpLimits(**kwargs)

    def test_control_supports_only_real_manager_context(self):
        template = '<script id="minor_buttons-tmpl" type="text/x-jquery-tmpl">' + CONTROL.split('>', 1)[1][:-6] + '</script>'
        self.assertTrue(_BumpControls(CONTROL).valid)
        self.assertTrue(_BumpControls(template).valid)
        for text in ('update_bump()', '<script>update_bump()</script>',
                     CONTROL + CONTROL, '<table>' + CONTROL + '</table>',
                     '<template>' + CONTROL + '</template>', CONTROL.replace('<button ', '<button disabled '),
                     CONTROL.replace('update_bump()', 'update_bump();other()'),
                     CONTROL.replace('useradmin_btnbox', 'article'),
                     CONTROL.replace('<button ', '<button onclick="other()" '),
                     template.replace('minor_buttons-tmpl', 'untrusted'), template.replace('끌올', '${unsafe}')):
            with self.subTest(text=text):
                self.assertFalse(_BumpControls(text).valid)

    def test_response_requires_exact_positive_message_and_no_contradictions(self):
        for body in (SUCCESS, '{"msg":"끌올되었습니다."}'):
            self.assertIsNone(assess_bump_response(response(body)))
        for body in ('{}', '[]', 'null', 'true', '', '<html>끌올되었습니다.</html>',
                     '{"result":"success"}', '{"msg":"완료"}', '{"msg":null}',
                     '{"result":"fail","msg":"끌올되었습니다."}',
                     '{"result":false,"msg":"끌올되었습니다."}',
                     '{"msg":"fail","msg":"끌올되었습니다."}',
                     '{"msg":"끌올되었습니다.","extra":1}',
                     '{"msg":"끌올되었습니다.","result":NaN}'):
            with self.subTest(body=body):
                self.assertIsNotNone(assess_bump_response(response(body)))
        for status, reason in ((401, 'auth_required'), (403, 'forbidden'), (429, 'rate_limited'),
                               (302, 'response_unknown'), (500, 'response_unknown')):
            self.assertEqual(assess_bump_response(response(SUCCESS, status)), reason)

    def test_exact_payload_sequential_limit_and_one_shot_runner(self):
        stats, runner = self.run_bumps()
        self.assertEqual((stats.requests, stats.acknowledged, stats.stopped), (4, 4, 'request_limit'))
        starts = [item[0] for item in self.session.posts]
        self.assertEqual(starts, [0, .5, 1, 1.5])
        for _, url, kwargs in self.session.posts:
            self.assertEqual(url, BUMP_URL)
            self.assertEqual(kwargs['form'], [('ci_t', 'synthetic-dummy-token'), ('id', 'mnt'),
                                             ('_GALLTYPE_', 'M'), ('nos[]', '647172')])
            self.assertEqual(kwargs['headers']['Referer'], TARGET.list_url)
        self.assertNotIn('synthetic-dummy-token', repr(stats))
        with self.assertRaises(RuntimeError):
            runner.run()

    def test_unknown_response_or_timeout_stops_without_second_request(self):
        for reply in (response('{}'), TimeoutError('synthetic-secret-error'), response('<html>Login</html>')):
            with self.subTest(reply=type(reply)):
                self.session.posts.clear()
                self.session.reply = reply
                stats, _ = self.run_bumps()
                self.assertEqual((stats.requests, stats.unknown, stats.stopped), (1, 1, 'response_unknown'))
                self.assertEqual(len(self.session.posts), 1)
                self.assertNotIn('synthetic-secret-error', repr(stats))

    def test_rate_limit_and_explicit_rejection_stop(self):
        for reply, reason in ((response(SUCCESS, 429), 'rate_limited'),
                              (response('{"result":"fail","msg":"거부"}'), 'rejection_reported')):
            self.session.reply = reply
            stats, _ = self.run_bumps()
            self.assertEqual((stats.requests, stats.acknowledged, stats.stopped), (1, 0, reason))

    def test_cancel_before_login_checks_and_during_interval(self):
        self.cancel.set()
        stats, _ = self.run_bumps()
        self.assertEqual(stats.stopped, 'user_stopped')
        self.assertFalse(self.session.calls)
        self.cancel.flag = False
        self.cancel.on_wait = self.cancel.set
        stats, _ = self.run_bumps()
        self.assertEqual((stats.requests, stats.stopped), (1, 'user_stopped'))

    def test_stop_during_request_keeps_acknowledgement_but_no_followup(self):
        self.session.before_post = self.cancel.set
        stats, _ = self.run_bumps()
        self.assertEqual((stats.requests, stats.acknowledged, stats.stopped), (1, 1, 'user_stopped'))

    def test_slow_requests_never_catch_up_or_exceed_duration(self):
        self.session.latency = 1.2
        stats, _ = self.run_bumps(limits=BumpLimits(duration=2, max_requests=10))
        self.assertEqual([x[0] for x in self.session.posts], [0, 1.2])
        self.assertEqual(stats.stopped, 'duration_limit')
        # Already sent work may finish after the deadline; no new send after it.
        self.assertAlmostEqual(stats.elapsed, 2.4)

    def test_session_preparation_latency_cannot_shorten_actual_send_interval(self):
        def delayed_token(*args):
            if not self.session.posts:
                self.clock.now += .4
            return 'synthetic-dummy-token'
        self.session.cookie_value = delayed_token
        stats, _ = self.run_bumps()
        starts = [x[0] for x in self.session.posts]
        self.assertAlmostEqual(starts[0], .4)
        self.assertTrue(all(b - a >= .5 - 1e-10 for a, b in zip(starts, starts[1:])))
        self.assertEqual(stats.acknowledged, 4)

    def test_full_two_hour_schedule_uses_bounded_memory_and_refreshes(self):
        stats, _ = self.run_bumps(limits=BumpLimits(duration=7200, max_requests=14400))
        self.assertEqual((stats.requests, stats.acknowledged), (14400, 14400))
        self.assertEqual(len([c for c in self.session.calls if c == ('GET', DEFAULT_RETURN_URL)]), 240)
        self.assertAlmostEqual(stats.rate, 2, places=3)
        self.assertEqual(self.session.posts[-1][0], 7199.5)

    def test_periodic_permission_loss_stops_before_another_send(self):
        def progress(stats):
            if stats.requests == 60:
                self.session.listing = DEMO_HTML
        stats, _ = self.run_bumps(limits=BumpLimits(duration=90, max_requests=180), progress=progress)
        self.assertEqual((stats.requests, stats.stopped), (60, 'forbidden'))

    def test_title_change_stops_but_reply_changes_do_not(self):
        for original, changed, reason in (('synthetic notice text', 'other title', 'target_changed'),
                                          ('댓글 0', '댓글 1', 'request_limit')):
            self.session.view = VIEW
            def progress(stats):
                if stats.requests == 60:
                    self.session.view = VIEW.replace(original, changed)
            stats, _ = self.run_bumps(limits=BumpLimits(duration=90, max_requests=61), progress=progress)
            self.assertEqual(stats.stopped, reason)

    def test_wrong_identity_missing_control_and_wrong_target_never_post(self):
        for attr, changed, reason in (('menu', MENU.replace(ACCOUNT, 'other_user'), 'auth_required'),
                                      ('listing', DEMO_HTML, 'forbidden'),
                                      ('view', VIEW.replace('647172', '647173'), 'target_unverified')):
            old = getattr(self.session, attr)
            setattr(self.session, attr, changed)
            stats, _ = self.run_bumps()
            self.assertEqual((stats.requests, stats.stopped), (0, reason))
            setattr(self.session, attr, old)
        self.assertFalse(self.session.posts)

    def test_deadline_after_preparation_blocks_post(self):
        adapter = BumpAdapter(self.login, TARGET, ACCOUNT, clock=self.clock)
        adapter.prepare(self.cancel, 1)
        self.clock.now = 1
        self.assertEqual(adapter.send(self.cancel, 1), BumpAttempt(False, 'duration_limit'))
        self.assertFalse(self.session.posts)

    def test_adapter_rejects_other_thread_and_unverified_login(self):
        with self.assertRaises(BumpStopped):
            BumpAdapter(replace(self.login, verified_user_id='other'), TARGET, ACCOUNT)
        adapter = BumpAdapter(self.login, TARGET, ACCOUNT, clock=self.clock)
        errors = []
        def call():
            try:
                adapter.prepare(self.cancel, 5)
            except BumpStopped as exc:
                errors.append(str(exc))
        t = Thread(target=call)
        t.start()
        t.join(2)
        self.assertEqual(errors, ['worker_failed'])
        self.assertFalse(self.session.calls)
