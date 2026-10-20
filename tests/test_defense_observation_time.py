from concurrent.futures import Future
from unittest import TestCase
from unittest.mock import Mock, patch

from rectifier.auth import AuthStatus, DEFAULT_RETURN_URL, LoginResult
from rectifier.batch_journal import BatchResponse
from rectifier.defense_flow import RefreshingDefenseFlow
from rectifier.defense_runner import DefenseRunner, ListDefenseAdapter
from tests.common import listing
from tests.test_defense_flow import guest
from tests.common import CONTROL, MENU, response


class ObservationTimeTests(TestCase):
    def test_aged_prefetch_never_replays_success_at_cooldown_boundary(self):
        for age in (1.5,2.,2.5,3.1):
            with self.subTest(age=age), patch('socket.socket',side_effect=AssertionError('offline')):
                now, remaining, posts, old = [0.], {101,102}, [], []
                session = Mock()
                session.cookie_value.return_value = 'synthetic'
                login = LoginResult(AuthStatus.VERIFIED,'synthetic',session,'syntheticadmin')
                adapter = ListDefenseAdapter(login,'mnt',clock=lambda:now[0])
                reader = adapter.new_reader()
                runner = DefenseRunner(adapter,after_post_id=1,clock=lambda:now[0])
                def request(method,url,**kwargs):
                    if method == 'GET':
                        return response(MENU if url==DEFAULT_RETURN_URL else listing(sorted(remaining))+CONTROL)
                    ids = tuple(int(v) for k,v in kwargs['form'] if k=='nos[]')
                    posts.append(ids)
                    if len(posts)==1:
                        old.append(reader.read_snapshot(runner.cancel))
                    remaining.difference_update(ids)
                    return response('{"result":"success"}')
                session.request.side_effect = request
                runner.step()
                now[0] = age
                future = Future()
                future.set_result(old[0])
                runner._prefetch = (reader,future)
                runner.step()
                self.assertEqual(posts,[(101,102)])

    def test_old_snapshot_keeps_completed_ids_excluded_but_accepts_new_ids(self):
        now = [0.]
        flow = RefreshingDefenseFlow('fixture',clock=lambda:now[0])
        flow.observe([guest(1)])
        batch = flow.take_batch()
        flow.record_response(batch,BatchResponse.SUCCESS_REPORTED)
        now[0] = 2.5
        flow.observe([guest(1),guest(2)],observed_at=.1)
        self.assertEqual(flow.take_batch().post_ids,(2,))

    def test_unapplied_unknown_requires_read_started_after_expiry(self):
        now = [0.]
        flow = RefreshingDefenseFlow('fixture',clock=lambda:now[0])
        flow.observe([guest(1)])
        flow.record_response(flow.take_batch(),BatchResponse.UNKNOWN)
        now[0] = 2.5
        flow.observe([guest(1)],observed_at=1.)
        self.assertIsNone(flow.take_batch())
        flow.observe([guest(1)],observed_at=2.5)
        self.assertEqual(flow.take_batch().post_ids,(1,))

    def test_bad_observation_time_fails_before_releasing_ids(self):
        for observed in (True,float('nan'),float('inf'),3.):
            flow = RefreshingDefenseFlow('fixture',clock=lambda:2.)
            with self.assertRaises(ValueError):
                flow.observe([guest(1)],observed_at=observed)
            self.assertEqual(flow.stats.stopped,'invalid_listing')
