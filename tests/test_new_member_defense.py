from dataclasses import replace
from threading import Event
from unittest import TestCase
from unittest.mock import Mock, patch

from rectifier.auth import AuthStatus, DEFAULT_RETURN_URL, LoginResult
from rectifier.defense_runner import DefenseDispatch, DefenseRunner, DefenseRunStats, ListDefenseAdapter
from rectifier.defense_task import DefenseTask
from rectifier.models import AuthorKind
from rectifier.page_preview import classify_list_entries
from rectifier.parser import parse_board_entries
from rectifier.policy import DeletePolicy
from tests.common import ACCOUNT
from tests.common import listing
from tests.test_parser import board, post_row
from tests.common import CONTROL, MENU, response


BASE = 'https://nstatic.dcinside.com/dc/w/images/'


def member_row(pid, icon='newnik.gif', *, kind='icon_txt', writer='data-uid="synthetic_member"',
               badge=None):
    html = post_row(writer=writer, number=str(pid), attrs=f'data-type="{kind}" data-no="{pid}"',
        title='synthetic', link=f'/mgallery/board/view/?id=mnt&amp;no={pid}')
    markup = f'<a class="writer_nikcon "><img src="{BASE}{icon}" width="12" height="11"></a>'
    return html.replace('synthetic nickname', 'synthetic nickname' + (markup if badge is None else badge))


class NewMemberBadgeTests(TestCase):
    def setUp(self):
        blocked = patch('socket.socket', side_effect=AssertionError('offline only'))
        blocked.start()
        self.addCleanup(blocked.stop)

    def test_both_badges_are_members_and_opt_in_only(self):
        for name in ('newnik.gif', 'fix_newnik.gif'):
            with self.subTest(icon=name):
                entries = parse_board_entries(board(member_row(101, name)), 'mnt')
                self.assertIs(entries[0].post.author_kind, AuthorKind.MEMBER)
                self.assertTrue(entries[0].new_member_badge)
                default = classify_list_entries('mnt', entries)
                enabled = classify_list_entries('mnt', entries, include_new_members=True)
                self.assertEqual(default['batch_candidate_count'], 0)
                self.assertEqual(default['counts']['protected_member'], 1)
                self.assertEqual(enabled['batch_candidate_count'], 1)
                self.assertEqual(enabled['counts']['delete_new_member'], 1)

    def test_only_official_icon_in_unambiguous_writer_badge_is_used(self):
        valid = '<a class="writer_nikcon"><img src="' + BASE + 'newnik.gif"></a>'
        invalid = (
            '', '<img src="' + BASE + 'newnik.gif">',
            valid.replace('newnik.gif', 'nik.gif'),
            valid.replace('newnik.gif', 'fix_nik.gif'),
            valid.replace('newnik.gif', 'fix_managernik.gif'),
            valid.replace('newnik.gif', 'bestcon_fix.png'),
            valid.replace('newnik.gif', 'unknown_newnik.gif'),
            valid.replace('nstatic.dcinside.com', 'untrusted.invalid'),
            valid.replace('nstatic.dcinside.com', 'nstatic.dcinside.com@untrusted.invalid'),
            valid.replace('<img ', '<img src="ignored" '),
            valid + valid,
            valid.replace('</a>', '<img src="' + BASE + 'fix_sub_managernik.gif"></a>'),
        )
        for badge in invalid:
            with self.subTest(badge=badge):
                entries = parse_board_entries(board(member_row(101, badge=badge)), 'mnt')
                self.assertFalse(entries[0].new_member_badge)
                self.assertEqual(classify_list_entries('mnt', entries,
                    include_new_members=True)['batch_candidate_count'], 0)
        # A matching image elsewhere in the row cannot classify its author.
        html = member_row(101, badge='').replace('>synthetic</a>',
            '>synthetic<img src="' + BASE + 'newnik.gif"></a>')
        self.assertFalse(parse_board_entries(board(html), 'mnt')[0].new_member_badge)
        html = member_row(101).replace('https://nstatic', '//nstatic')
        self.assertTrue(parse_board_entries(board(html), 'mnt')[0].new_member_badge)

    def test_unknown_author_and_cross_page_badge_changes_do_not_become_candidates(self):
        unknown = parse_board_entries(board(member_row(101, writer='')), 'mnt')
        self.assertFalse(unknown[0].new_member_badge)
        yes = parse_board_entries(board(member_row(102)), 'mnt')[0]
        no = replace(yes, new_member_badge=False)
        for entries in ((yes, no), (no, yes), unknown):
            self.assertEqual(classify_list_entries('mnt', entries,
                include_new_members=True)['batch_candidate_count'], 0)
        bad_kind = parse_board_entries(board(member_row(103, kind='unexpected')), 'mnt')
        self.assertEqual(classify_list_entries('mnt', bad_kind,
            include_new_members=True, include_recommended=True)['batch_candidate_count'], 0)

    def test_pipeline_uses_checkbox_policy_and_preserves_cutoff_and_single_recommended_requests(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                now = [0.0]
                page = [listing((101,)) + board(
                    member_row(99), member_row(102), member_row(103, 'fix_newnik.gif'),
                    member_row(104, kind='icon_recomtxt'), member_row(105, 'fix_nik.gif'),
                    member_row(106, kind='icon_notice')) + CONTROL]
                # One valid table per page, with the guest and member rows combined.
                page[0] = page[0].replace('</tbody></table><table class="gall_list"><tbody>', '')
                session = Mock()
                session.cookie_value.return_value = 'synthetic-token'
                def request(method, url, **kwargs):
                    if method == 'POST':
                        return response('{"result":"success"}')
                    return response(MENU if url == DEFAULT_RETURN_URL else page[0])
                session.request.side_effect = request
                login = LoginResult(AuthStatus.VERIFIED, 'synthetic', session, ACCOUNT)
                adapter = ListDefenseAdapter(login, 'mnt', clock=lambda: now[0],
                    include_new_members=enabled, include_recommended=True)
                self.assertEqual(adapter.new_reader().include_new_members, enabled)
                runner = DefenseRunner(adapter, after_post_id=100, clock=lambda: now[0],
                                       delete_workers=12, posts_per_request=5)
                runner.step()
                posts = [c for c in session.request.call_args_list if c.args[0] == 'POST']
                parts = [[v for k, v in c.kwargs['form'] if k == 'nos[]'] for c in posts]
                expected = {'101', '102', '103', '104'} if enabled else {'101'}
                self.assertEqual({pid for part in parts for pid in part}, expected)
                self.assertEqual(sum(map(len, parts)), len(expected))
                self.assertIsNone(runner.stats.stopped)
                if enabled:
                    self.assertIn(['104'], parts)
                self.assertEqual(sum(c.args[0] == 'GET' for c in session.request.call_args_list), 2)
                # After cooldown a now-normal badge must no longer qualify.
                now[0] += 5
                page[0] = board(member_row(102, 'nik.gif')) + CONTROL
                runner.step()
                self.assertEqual(runner.stats.accepted_unverified, len(expected))

    def test_task_transmits_option_and_manual_adapter_cannot_enable_it(self):
        session = Mock()
        session.request.side_effect = lambda method, url, **kw: response(
            MENU if url == DEFAULT_RETURN_URL else listing((100,)) + CONTROL)
        login = LoginResult(AuthStatus.VERIFIED, 'synthetic', session, ACCOUNT)
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                runner = Mock()
                runner.run.return_value = DefenseRunStats(0, 0, 0, 0, 0, 'user_stopped', 0)
                with patch('rectifier.defense_task.DefenseRunner', return_value=runner) as factory:
                    task = DefenseTask('mnt', (1,), login_result=login, include_new_members=enabled)
                    task._work()
                self.assertEqual(factory.call_args.args[0].include_new_members, enabled)
                self.assertEqual(factory.call_args.kwargs['after_post_id'], 100)
        with self.assertRaises(ValueError):
            ListDefenseAdapter(login, 'mnt', policy=DeletePolicy.MANUAL_SELECTED, include_new_members=True)
        with self.assertRaises(ValueError):
            DefenseTask('mnt', (1,), login_result=login, include_new_members='yes')

    def test_disabled_adapter_cannot_send_new_member_even_if_given_its_id(self):
        session = Mock()
        session.request.side_effect = lambda method, url, **kw: response(
            MENU if url == DEFAULT_RETURN_URL else board(member_row(101)) + CONTROL)
        adapter = ListDefenseAdapter(LoginResult(AuthStatus.VERIFIED, 'synthetic', session, ACCOUNT), 'mnt')
        snapshot = adapter.read_snapshot(Event())
        receipt = adapter.send(DefenseDispatch('mnt', ACCOUNT, (101,)), snapshot, Event())
        self.assertEqual(receipt.stopped, 'scope_mismatch')
        self.assertFalse(receipt.attempted)
        self.assertTrue(all(c.args[0] == 'GET' for c in session.request.call_args_list))
