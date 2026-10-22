from pathlib import Path
from queue import Queue
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic, sleep
import tkinter as tk
from unittest import TestCase
from unittest.mock import Mock, patch

from rectifier.account_profiles import AccountProfiles, GalleryStore
from rectifier.account_task import LoginTask
from rectifier.auth import AuthStatus, AuthStep, LoginResult
from rectifier.batch_journal import BatchResponse
from rectifier.defense_runner import DispatchReceipt
from rectifier.defense_task import TaskState
from rectifier.dpi import apply_theme
from rectifier.manager_ui import ManagerUI
from rectifier.page_preview import preview_pages
from rectifier.public_board import PublicServerError
from tests.test_account_manual import ACCOUNT
from tests.common import listing


class ManagerUITests(TestCase):
    def setUp(self):
        self.enterContext(patch('rectifier.defense_monitor.write_summary', return_value=True))
        self.block = patch('socket.socket', side_effect=AssertionError('offline UI only'))
        self.block.start()
        self.root = tk.Tk()
        self.root.withdraw()
        self.session = Mock()
        self.login = LoginResult(AuthStatus.VERIFIED, 'synthetic', self.session, ACCOUNT)
        self.action = Mock()
        self.action.execute.return_value = DispatchReceipt(BatchResponse.SUCCESS_REPORTED, True)
        self.factory = Mock(return_value=self.action)
        self.ui = ManagerUI(self.root, action_factory=self.factory)

    def tearDown(self):
        self.ui.close()
        deadline = monotonic() + 2
        while not self.ui.closed and monotonic() < deadline:
            self.root.update()
            sleep(.01)
        self.block.stop()
        self.assertTrue(self.ui.closed)

    def wait(self, condition):
        deadline = monotonic() + 3
        while not condition() and monotonic() < deadline:
            self.root.update()
            sleep(.01)
        self.assertTrue(condition())

    def select(self):
        self.ui.gallery.set('mnt')
        generation = self.ui.model.begin('mnt', (1,))
        self.ui.model.apply(generation, preview_pages('mnt', ACCOUNT, (1,), reader=lambda *_: listing()))
        self.ui._render()
        self.ui.select('guests')

    def authenticate(self):
        self.ui.login_result = self.login
        self.ui._delete_state(None)

    def test_list_query_names_dcinside_outage(self):
        def reader(gallery, page):
            if page == 2:
                raise PublicServerError('HTTP 504')
            return listing()
        generation = self.ui.model.begin('mnt', (1, 2))
        self.ui.messages.put((generation, preview_pages('mnt', 'ui_preview', (1, 2), reader=reader)))
        self.ui._poll_query()
        self.assertIn('일부 조회', self.ui.status.get())
        self.assertIn('서버 장애', self.ui.detail.get())

    def test_open_is_inert_and_no_hardcoded_account(self):
        self.assertEqual(self.ui.account_text.get(), '')
        self.assertEqual(self.ui.password_entry.cget('show'), '●')
        self.assertFalse(hasattr(self.ui, 'preserve_box'))
        self.assertFalse(hasattr(self.ui, 'saved_box'))
        self.session.request.assert_not_called()
        self.assertEqual(str(self.ui.logout_button.cget('state')), 'disabled')

    def test_manual_and_defense_require_login(self):
        self.select()
        with patch('rectifier.manager_ui.messagebox.showwarning') as warning:
            self.ui.prepare_delete()
            self.ui.show_defense()
            self.ui.prepare_release()
            self.ui.check_permission()
            self.assertEqual(warning.call_count, 4)
            self.assertTrue(all(call.args[0] == '로그인 필요' for call in warning.call_args_list))
            self.assertTrue(all(call.kwargs['parent'] is self.root for call in warning.call_args_list))
        self.factory.assert_not_called()
        self.assertIsNone(self.ui.defense_window)

    def test_new_account_login_password_cleared_and_permission_checked(self):
        login_fn = Mock(return_value=self.login)
        self.ui.login_task_factory = lambda account, password, **kwargs: LoginTask(
            account, password, login_fn=login_fn, **kwargs)
        self.ui.account_text.set(ACCOUNT)
        self.ui.password_text.set('synthetic-password')
        with patch.object(self.ui, 'check_permission') as check:
            self.ui.sign_in()
            self.assertEqual(self.ui.password_text.get(), '')
            self.wait(lambda: self.ui.login_task is None)
            check.assert_called_once()
        self.assertIn(ACCOUNT, self.ui.account_status.get())
        self.assertIs(self.ui.login_result, self.login)

    def test_manual_confirm_consumed_once_and_no_required_archive(self):
        self.authenticate()
        self.select()
        self.ui.prepare_delete()
        self.assertEqual(self.ui.delete_phase, 'ready')
        self.action.execute.assert_not_called()
        self.ui.confirm_delete()
        self.ui.confirm_delete()
        self.wait(lambda: self.ui.delete_phase is None)
        self.action.execute.assert_called_once()
        self.action.execute.assert_called_once_with(self.ui.delete_cancel)
        self.assertFalse(self.ui.model.selected)
        self.assertIn('수락', self.ui.detail.get())

    def test_manual_delete_auto_requeries_remaining_posts_while_keeping_result_card(self):
        self.ui.reader = Mock(return_value=listing((301, 302)))
        self.authenticate()
        self.select()
        self.ui.prepare_delete()
        self.assertEqual(self.ui.delete_phase, 'ready')
        self.action.execute.assert_not_called()
        self.ui.confirm_delete()
        self.wait(lambda: self.ui.delete_phase is None)
        self.ui.reader.assert_called_once_with('mnt', 1)
        self.assertEqual(set(self.ui.model.rows), {301, 302})
        self.assertEqual(self.ui.tree.get_children(), ('301', '302'))
        self.assertFalse(self.ui.model.selected)
        self.assertIsNotNone(self.ui.review_window)
        self.assertIn('수락', self.ui.detail.get())

    def test_login_failure_shows_diagnostic_and_allows_new_attempt(self):
        result = LoginResult(AuthStatus.UNSUPPORTED, 'synthetic',
                             diagnostics=(AuthStep('credentials', 500),))
        login_fn = Mock(return_value=result)
        self.ui.login_task_factory = lambda account, password, **kwargs: LoginTask(
            account, password, login_fn=login_fn, **kwargs)
        self.ui.account_text.set(ACCOUNT)
        self.ui.password_text.set('synthetic-password')
        self.ui.sign_in()
        self.wait(lambda: self.ui.login_task is None)
        self.assertIn('계정 제출 · HTTP 500', self.ui.account_status.get())
        self.assertNotIn('synthetic', self.ui.account_status.get())
        self.assertEqual(str(self.ui.login_button.cget('state')), 'normal')
        self.assertIsNone(self.ui.login_result)
        login_fn.assert_called_once()

    def test_cancel_confirm_sends_nothing(self):
        self.authenticate()
        self.select()
        self.ui.prepare_delete()
        self.root.update_idletasks()
        opened = self.ui.review_container.winfo_reqheight()
        self.ui.cancel_delete()
        self.root.update_idletasks()
        self.assertLess(self.ui.review_container.winfo_reqheight(), opened // 4)
        self.ui.confirm_delete()
        self.action.execute.assert_not_called()
        self.assertIsNone(self.ui.delete_phase)

    def test_permission_check_is_read_only_and_scoped(self):
        self.authenticate()
        self.ui.gallery.set('mnt')
        self.ui.pages.set('1')
        adapter = Mock()
        with patch('rectifier.manager_ui.ListDefenseAdapter', return_value=adapter) as adapter_type:
            adapter.gallery = 'mnt'
            self.ui.check_permission()
            self.wait(lambda: self.ui.delete_phase is None)
            adapter_type.assert_called_once_with(self.login, 'mnt', pages=(1,), max_age=60)
            adapter.read_snapshot.assert_called_once()
            adapter.send.assert_not_called()
        self.assertIn('mnt 관리 삭제 권한 확인됨', self.ui.permission_status.get())
        self.ui.gallery.set('othergallery')
        self.assertIn('미확인', self.ui.permission_status.get())

    def test_select_all_skips_recommended_and_unknown(self):
        from tests.test_manual_ui import result
        self.ui.gallery.set('fixture')
        generation = self.ui.model.begin('fixture', (1,))
        data = result()
        data['items'][0]['post_kind'] = 'ordinary'
        data['items'][1]['post_kind'] = 'ordinary'
        data['items'][2]['post_kind'] = 'recommended'
        self.ui.model.apply(generation, data)
        self.ui._render()
        self.ui.select('all')
        self.assertTrue(self.ui.model.selected)
        self.assertTrue(all(row.post_kind == 'ordinary' and row.disposition in
                            {'delete_guest', 'protected_member'} for row in self.ui.model.review().rows))

    def test_logout_locked_while_manual_worker_uses_session_and_close_waits(self):
        entered, release = Event(), Event()
        def execute(cancel, **kwargs):
            entered.set()
            release.wait(2)
            return DispatchReceipt(BatchResponse.UNKNOWN, True)
        self.action.execute.side_effect = execute
        self.authenticate()
        self.select()
        self.ui.prepare_delete()
        self.ui.confirm_delete()
        self.assertTrue(entered.wait(1))
        self.ui.sign_out()
        self.ui.close()
        self.assertFalse(self.ui.closed)
        self.assertTrue(self.ui.delete_cancel.is_set())
        self.session.close.assert_not_called()
        release.set()
        self.wait(lambda: self.ui.closed)
        self.session.close.assert_called_once()

    def test_logout_clears_selection_and_permissions(self):
        self.authenticate()
        self.select()
        self.ui.permission_status.set('mnt 권한 확인됨')
        self.ui.sign_out()
        self.assertIsNone(self.ui.login_result)
        self.assertFalse(self.ui.model.rows)
        self.assertIn('미확인', self.ui.permission_status.get())
        self.session.close.assert_called_once()

    def test_defense_factory_borrows_current_session(self):
        self.authenticate()
        self.ui.gallery.set('mnt')
        # Even invalid manual input must not redirect or block defense.
        self.ui.pages.set('1500, invalid')
        with patch('rectifier.manager_ui.DefenseTask') as task_type:
            task = task_type.return_value
            task.alive = False
            task.state = TaskState()
            task.captchas = Queue()
            task.cancel = Event()
            self.ui.show_defense()
            window = self.ui.defense_window
            self.assertEqual(window.pages, (1,))
            window.page_count.set('5')
            window.start()
            task_type.assert_called_once_with('mnt', (1, 2, 3, 4, 5), login_result=self.login, include_new_members=False)
            window.close()
        self.session.close.assert_not_called()

    def test_top_defense_button_starts_and_stops_without_leaving_the_list(self):
        self.authenticate()
        self.ui.gallery.set('mnt')
        with patch('rectifier.manager_ui.DefenseTask') as task_type:
            task = task_type.return_value
            task.alive = True
            task.state = TaskState('running')
            task.captchas = Queue()
            task.cancel = Event()
            self.ui.defense_button.invoke()
            # One click from the list tab, with the defense tab's default settings.
            task_type.assert_called_once_with('mnt', (1,), login_result=self.login, include_new_members=False)
            self.assertEqual(self.ui.delete_phase, 'defense')
            self.assertEqual(self.ui.notebook.select(), str(self.ui.list_tab))
            self.assertEqual(self.ui.defense_button.cget('text'), '방어 끄기')
            self.assertEqual(str(self.ui.defense_button.cget('state')), 'normal')
            self.assertEqual(str(self.ui.logout_button.cget('state')), 'disabled')
            self.wait(lambda: '방어 실행 중' in self.ui.defense_state.get())
            self.ui.defense_button.invoke()
            task.stop.assert_called_once_with()
            task.alive = False
            task.state = TaskState('finished', reason='request_limit')
            self.wait(lambda: self.ui.delete_phase is None)
        self.assertEqual(self.ui.defense_button.cget('text'), '방어 켜기')
        self.assertEqual(self.ui.defense_state.get(), '● 방어 꺼짐')
        self.assertEqual(self.ui.notebook.tab(self.ui.defense_tab, 'text').strip(), '자동 방어')
        self.session.close.assert_not_called()

    def test_top_defense_button_requires_login_and_waits_for_query(self):
        with patch('rectifier.manager_ui.messagebox.showwarning') as warning:
            self.ui.defense_button.invoke()
        warning.assert_called_once()
        self.assertIsNone(self.ui.defense_window)
        self.authenticate()
        self.ui.busy = True
        self.ui.defense_button.invoke()
        self.assertIsNone(self.ui.defense_window)
        self.assertIn('조회가 끝난 뒤', self.ui.status.get())
        self.ui.busy = False

    def test_range_buttons_read_common_ranges_in_one_click(self):
        self.assertEqual([button.cget('text') for button in self.ui.range_buttons],
                         ['최신 1페이지', '1~3페이지', '1~5페이지'])
        with patch.object(self.ui, 'query') as query:
            self.ui.range_buttons[1].invoke()
            self.assertEqual(self.ui.pages.get(), '1,2,3')
            self.ui.range_buttons[0].invoke()
            self.assertEqual(self.ui.pages.get(), '1')
        self.assertEqual(query.call_count, 2)

    def test_delete_key_does_what_the_delete_button_allows(self):
        self.authenticate()
        self.select()
        self.assertIn('<Key-Delete>', self.ui.tree.bind())
        self.ui.model.selected.clear()
        self.ui._refresh_selection()
        self.ui._delete_key()
        self.assertIsNone(self.ui.delete_phase)  # Nothing selected: the button is off, so is the key.
        self.ui.select('guests')
        self.ui._delete_key()
        self.assertEqual(self.ui.delete_phase, 'ready')  # Same confirm step as the button.
        self.ui.cancel_delete()
        self.action.execute.assert_not_called()

    def test_logo_fills_the_title_and_gallery_rows_without_growing_them(self):
        from rectifier.branding import LOGO_SIZES, logo_path
        self.assertTrue(all(logo_path(size).is_file() for size in LOGO_SIZES))
        self.root.update_idletasks()
        self.ui._fit_logo()
        header, context_row = self.ui.logo_rows
        room = header.winfo_reqheight() + context_row.winfo_reqheight() + self.ui.px(6) + self.ui.px(4)
        size = self.ui.logo_size
        self.assertLessEqual(size, room)
        self.assertTrue(all(larger > room for larger in LOGO_SIZES if larger > size))
        self.assertEqual(int(self.ui.brand_logo.grid_info()['rowspan']), 2)

    def test_no_sideways_table_scroll_and_plain_tab_names(self):
        self.assertEqual(str(self.ui.tree.cget('xscrollcommand')), '')
        # Emoji tab icons rendered as broken glyphs under Tk on Windows.
        for tab in (self.ui.list_tab, self.ui.defense_tab, self.ui.bump_tab):
            text = self.ui.notebook.tab(tab, 'text')
            self.assertFalse(any(ord(ch) > 0xFFFF or 0x2600 <= ord(ch) <= 0x27BF or ch == '️'
                                 for ch in text), text)

    def test_bump_requires_login_and_locks_only_while_running(self):
        with patch('rectifier.manager_ui.messagebox.showwarning') as warning:
            self.ui.show_bump()
        warning.assert_called_once()
        self.assertIsNone(self.ui.bump_window)
        self.authenticate()
        task = Mock(alive=True, state=TaskState('running'), captchas=Queue(), cancel=Event(),
                    title='', pair_progress=None)
        factory = Mock(return_value=task)
        self.ui.bump_window_factory = lambda parent, on_closed, **options: __import__(
            'rectifier.bump_ui', fromlist=['BumpWindow']).BumpWindow(parent, on_closed, task_factory=factory, **options)
        self.ui.show_bump()
        window = self.ui.bump_window
        window.window.withdraw()
        self.assertIs(window.login_result, self.login)
        # Viewing the bump tab alone does NOT lock the session (Proposal 1).
        self.assertIsNone(self.ui.delete_phase)
        self.assertEqual(str(self.ui.logout_button.cget('state')), 'normal')
        self.assertEqual(str(self.ui.gallery_box.cget('state')), 'normal')
        # The bump window hides account controls and never runs its own login.
        self.assertFalse(window.login_box.winfo_manager())
        window.sign_in()
        window.sign_out()
        self.assertIsNone(window.login_task)
        self.assertIs(window.login_result, self.login)
        # Starting a bump trial/repeat locks the session until the worker finishes.
        window.url.set('https://gall.dcinside.com/mgallery/board/view/?id=mnt&no=101')
        window.start(trial=True)
        self.assertEqual(self.ui.delete_phase, 'bump')
        for button in (self.ui.logout_button, self.ui.defense_button, self.ui.query_button):
            self.assertEqual(str(button.cget('state')), 'disabled')
        task.alive = False
        task.state = TaskState('finished', reason='request_limit')
        window.window.after_cancel(window.poll_id)
        window._poll()
        self.assertIsNone(self.ui.delete_phase)
        self.assertEqual(str(self.ui.logout_button.cget('state')), 'normal')
        window.close()
        self.assertIsNone(self.ui.bump_window)
        self.assertIsNone(self.ui.delete_phase)
        self.session.close.assert_not_called()

    def test_bump_window_dispatch_uses_borrowed_session(self):
        self.authenticate()
        task = Mock(alive=False, state=TaskState(), captchas=Queue(), cancel=Event(), title='')
        factory = Mock(return_value=task)
        self.ui.bump_window_factory = lambda parent, on_closed, **options: __import__(
            'rectifier.bump_ui', fromlist=['BumpWindow']).BumpWindow(parent, on_closed, task_factory=factory, **options)
        self.ui.show_bump()
        window = self.ui.bump_window
        window.window.withdraw()
        window.url.set('https://gall.dcinside.com/mgallery/board/view/?id=mnt&no=101')
        window.start(trial=True)
        self.assertIs(factory.call_args.kwargs['login_result'], self.login)
        self.assertIsNone(factory.call_args.kwargs['password'])
        window.close()
        self.session.close.assert_not_called()

    def test_main_close_closes_bump_window_first(self):
        self.authenticate()
        self.ui.show_bump()
        self.ui.bump_window.window.withdraw()
        self.ui.close()
        self.assertTrue(self.ui.closed)
        self.session.close.assert_called_once()

    def mixed(self):
        from tests.test_manual_ui import result
        self.ui.gallery.set('fixture')
        generation = self.ui.model.begin('fixture', (1,))
        data = result()
        for item, kind in zip(data['items'], ('ordinary', 'ordinary', 'recommended', 'ordinary', 'ordinary', 'recommended')):
            item['post_kind'] = kind
        self.ui.model.apply(generation, data)
        self.ui._render()
        return {row.post_id: row for row in self.ui.model.rows.values()}

    def test_selection_hint_explains_disabled_delete_and_release(self):
        rows = self.mixed()
        self.assertEqual(self.ui.selection_hint.get(), '')
        ordinary = next(pid for pid, row in rows.items() if row.post_kind == 'ordinary'
                        and row.disposition == 'delete_guest')
        recommended = next(pid for pid, row in rows.items() if row.post_kind == 'recommended'
                           and row.disposition == 'protected_member')
        unknown = next(pid for pid, row in rows.items() if row.disposition == 'unknown_author')
        self.ui._toggle(str(ordinary))
        self.assertIn('선택 삭제', self.ui.selection_hint.get())
        self.ui._toggle(str(recommended))
        self.assertIn('섞여 있음', self.ui.selection_hint.get())
        self.assertIn('disabled', self.ui.delete_button.state())
        self.assertIn('disabled', self.ui.release_button.state())
        self.ui._toggle(str(ordinary))
        self.assertIn('개념글 해제', self.ui.selection_hint.get())
        self.ui._toggle(str(unknown))
        self.assertIn('판별 불가', self.ui.selection_hint.get())

    def test_mixed_selection_helper_buttons_filter_without_auto_executing(self):
        self.authenticate()
        rows = self.mixed()
        ordinary = next(pid for pid, row in rows.items() if row.post_kind == 'ordinary'
                        and row.disposition == 'delete_guest')
        recommended = next(pid for pid, row in rows.items() if row.post_kind == 'recommended'
                           and row.disposition == 'protected_member')
        self.ui._toggle(str(ordinary))
        self.ui._toggle(str(recommended))
        self.assertIn('disabled', self.ui.delete_button.state())
        self.assertIn('disabled', self.ui.release_button.state())
        self.assertNotIn('disabled', self.ui.keep_ordinary_button.state())
        self.assertNotIn('disabled', self.ui.keep_recommended_button.state())
        self.ui.keep_ordinary_button.invoke()
        self.assertEqual(self.ui.model.selected, {ordinary})
        self.assertNotIn('disabled', self.ui.delete_button.state())
        self.assertIn('disabled', self.ui.release_button.state())
        self.ui._toggle(str(recommended))
        self.ui.keep_recommended_button.invoke()
        self.assertEqual(self.ui.model.selected, {recommended})
        self.assertNotIn('disabled', self.ui.release_button.state())
        self.assertIn('disabled', self.ui.delete_button.state())
        self.factory.assert_not_called()

    def test_send_selected_to_bump_fills_single_and_multi_urls_without_network(self):
        rows = self.mixed()
        pids = list(rows)[:3]
        self.ui._toggle(str(pids[0]))
        self.ui.send_to_bump_button.invoke()
        window = self.ui.bump_window
        self.assertIsNotNone(window)
        self.assertFalse(window.experimental)
        self.assertEqual(window.url.get(), f'https://gall.dcinside.com/mgallery/board/view/?id=fixture&no={pids[0]}')
        self.assertIsNone(window.task)
        self.assertIsNone(self.ui.delete_phase)
        self.assertEqual(self.ui.notebook.select(), str(self.ui.bump_tab))
        self.ui._toggle(str(pids[1]))
        self.ui._toggle(str(pids[2]))
        self.ui.send_to_bump_button.invoke()
        self.assertTrue(window.experimental)
        self.assertEqual(window.url.get(), f'https://gall.dcinside.com/mgallery/board/view/?id=fixture&no={pids[0]}')
        self.assertEqual(window.second_url.get(), f'https://gall.dcinside.com/mgallery/board/view/?id=fixture&no={pids[1]}')
        self.assertEqual(window.additional_urls.get('1.0', 'end-1c').strip(),
                         f'https://gall.dcinside.com/mgallery/board/view/?id=fixture&no={pids[2]}')
        self.assertIsNone(window.task)
        self.session.request.assert_not_called()

    def test_recent_galleries_persisted_in_separate_store_and_restored(self):
        with TemporaryDirectory() as tmp:
            store_path = Path(tmp) / 'recent-galleries.json'
            store = GalleryStore(store_path)
            self.ui.gallery_store = store
            self.ui.reader = Mock(return_value=listing((101,)).replace('id=mnt', 'id=customgall'))
            self.ui.gallery.set('customgall')
            self.ui.pages.set('1')
            self.ui.query()
            self.wait(lambda: not self.ui.busy)
            self.assertEqual(store.load(), ('customgall',))
            self.assertIn('customgall', self.ui.gallery_box.cget('values'))
            self.assertFalse(store.remember('bad/gallery;secret'))
            raw = store_path.read_text(encoding='utf-8')
            self.assertNotIn('secret', raw)

    def test_saved_account_blank_password_uses_saved_and_typed_password_overwrites(self):
        with TemporaryDirectory() as tmp:
            store = AccountProfiles(Path(tmp) / 'accounts.sqlite', password_writer=Mock())
            store.remember(ACCOUNT, 'saved-pw', auto_login=False)
            self.ui.profile_store = store
            self.ui._load_profiles()
            captured = []
            def fake_factory(account, password, **kwargs):
                captured.append((account, password, kwargs))
                task = Mock(alive=False, state=TaskState('finished'), captchas=Queue(),
                            cancel=Event(), profile_warning=None, profile_saved=True,
                            failure_message=None)
                task.take_result.return_value = self.login
                return task
            self.ui.login_task_factory = fake_factory
            self.ui.account_text.set(ACCOUNT)
            self.ui.password_text.set('')
            with patch.object(self.ui, 'check_permission'):
                self.ui.sign_in()
                self.ui._poll_login()
            self.assertEqual(captured[-1][1], '')
            self.assertTrue(captured[-1][2]['use_saved'])
            self.ui.sign_out()
            self.ui.account_text.set(ACCOUNT)
            self.ui.password_text.set('new-typed-password')
            with patch.object(self.ui, 'check_permission'):
                self.ui.sign_in()
                self.ui._poll_login()
            self.assertEqual(captured[-1][1], 'new-typed-password')
            self.assertFalse(captured[-1][2]['use_saved'])

    def test_theme_fallback_when_sv_ttk_fails(self):
        self.root._rectifier_theme_loaded = False
        with patch('sv_ttk.set_theme', side_effect=RuntimeError('theme load failure')):
            apply_theme(self.root)
        self.assertTrue(self.root.winfo_exists())

    def test_rows_are_coloured_by_author_and_kind(self):
        rows = self.mixed()
        for pid, row in rows.items():
            tags = set(self.ui.tree.item(str(pid), 'tags'))
            self.assertEqual('guest' in tags, row.disposition == 'delete_guest')
            self.assertEqual('recommended' in tags, row.post_kind == 'recommended')
            self.assertEqual('uncertain' in tags, row.disposition not in {'delete_guest', 'protected_member'})

    def test_account_inputs_collapse_after_login_and_return_after_logout(self):
        self.assertEqual(self.ui.login_inputs.winfo_manager(), 'grid')
        self.authenticate()
        self.assertEqual(self.ui.login_inputs.winfo_manager(), '')
        self.assertEqual(self.ui.login_options.winfo_manager(), '')
        self.ui.sign_out()
        self.assertEqual(self.ui.login_inputs.winfo_manager(), 'grid')
        self.session.close.assert_called_once()

    def test_bulk_button_labels_say_what_they_pick(self):
        labels = [button.cget('text') for button in self.ui.selection_buttons]
        self.assertEqual(labels, ['비회원 일반글 선택', '일반글 전체 선택 (회원 포함)', '선택 해제', '개념글만 선택'])

    def test_unknown_phase_is_rejected(self):
        with self.assertRaises(ValueError):
            self.ui._delete_state('preparing')
