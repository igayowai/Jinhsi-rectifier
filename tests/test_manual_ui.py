from dataclasses import FrozenInstanceError
from pathlib import Path
from threading import Event
from time import monotonic, sleep
import tkinter as tk
from unittest import TestCase
from unittest.mock import Mock, patch

from rectifier.page_preview import preview_pages
from rectifier.public_board import PublicReadError
from rectifier.selection import ManualSelection, parse_pages
from rectifier.manager_ui import ManagerUI


HTML = (Path(__file__).parent / 'fixtures/mixed_board.html').read_text(encoding='utf-8')
# Same rows marked as ordinary posts, so the main window's bulk buttons can pick them.
ORDINARY_HTML = HTML.replace('<tr class="us-post" ', '<tr class="us-post" data-type="icon_txt" ')


def result(gallery='fixture', pages=(1,)):
    return preview_pages(gallery, 'synthetic', pages,
                         reader=lambda *_: HTML.replace('id=fixture', f'id={gallery}'))


class SelectionTests(TestCase):
    def setUp(self):
        self.model = ManualSelection()
        self.generation = self.model.begin('fixture', (1,))
        self.model.apply(self.generation, result())

    def test_guest_and_all_are_distinct_and_clear_replaces(self):
        self.model.select('guests')
        self.assertEqual(len(self.model.selected), 2)
        self.assertTrue(all(r.disposition == 'delete_guest' for r in self.model.review().rows))
        self.model.select('all')
        self.assertEqual(len(self.model.selected), 6)
        self.assertEqual(sum(r.disposition == 'protected_member' for r in self.model.review().rows), 2)
        self.model.select('guests')
        self.assertEqual(len(self.model.selected), 2)
        self.model.select('none')
        self.assertFalse(self.model.selected)

    def test_individual_checkbox_and_missing_id(self):
        self.model.toggle(106)
        self.assertEqual(self.model.selected, {106})
        self.model.toggle(999)
        self.assertEqual(self.model.selected, {106})
        self.model.toggle(106)
        self.assertFalse(self.model.selected)

    def test_refresh_reordered_rows_never_moves_selection(self):
        self.model.toggle(106)
        generation = self.model.begin('fixture', (1,))
        updated = result()
        updated['items'].reverse()
        self.model.apply(generation, updated)
        self.assertFalse(self.model.selected)
        self.model.toggle(106)
        self.assertEqual(self.model.review().rows[0].post_id, 106)

    def test_gallery_change_with_identical_ids_and_delayed_response(self):
        self.model.select('all')
        generation = self.model.begin('mnt', (1,))
        self.assertFalse(self.model.apply(self.generation, result()))
        self.assertFalse(self.model.rows)
        self.model.apply(generation, result('mnt'))
        self.assertFalse(self.model.selected)
        self.assertEqual(self.model.review().gallery_id, 'mnt')

    def test_scope_mismatch_rejected(self):
        for changed in (result('mnt'), result(pages=(2,))):
            with self.assertRaises(ValueError):
                self.model.apply(self.generation, changed)

    def test_review_is_immutable_snapshot_without_aliases(self):
        self.model.select('all')
        review = self.model.review()
        self.model.invalidate()
        self.assertEqual(len(review.rows), 6)
        with self.assertRaises(FrozenInstanceError):
            review.rows[0].post_id = 999
        self.assertFalse(self.model.rows)
        self.assertFalse(self.model.selected)

    def test_duplicate_id_or_unrecognized_type_rejected(self):
        for key, value in [('post_id', True), ('post_kind', 'notice'), ('disposition', 'guest'), ('title', None)]:
            changed = result()
            changed['items'][0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.model.apply(self.generation, changed)
        changed = result()
        changed['items'].append(changed['items'][0])
        with self.assertRaises(ValueError):
            self.model.apply(self.generation, changed)

    def test_pages_input_accepts_commas_spaces_rejects_invalid(self):
        self.assertEqual(parse_pages(' 1, 2  3 '), (1, 2, 3))
        for text in ('', '0', '1 1', '1 2 3 4 5 6', '1000001', '-1', '1.5', '1;2', 'x'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_pages(text)


class ManagerBrowsingTests(TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError:
            self.skipTest('Tk display unavailable')
        self.root.withdraw()
        self.reader = Mock(return_value=ORDINARY_HTML)
        self.action = Mock()
        self.ui = ManagerUI(self.root, reader=self.reader, action_factory=self.action)
        self.ui.gallery.set('fixture')
        self.addCleanup(self.close)

    def close(self):
        if not self.ui.closed:
            self.ui.close()

    def drain(self):
        deadline = monotonic() + 3
        while self.ui.busy and monotonic() < deadline:
            self.root.update()
            sleep(.005)
        self.assertFalse(self.ui.busy, 'UI worker did not finish')

    def test_query_buttons_and_review_use_fixed_ids_without_auth_or_delete(self):
        with patch('rectifier.auth.login') as login, patch('rectifier.credentials.read_password') as password:
            self.ui.query_button.invoke()
            self.drain()
            self.assertEqual(len(self.ui.tree.get_children()), 6)
            self.ui.selection_buttons[0].invoke()
            self.assertEqual(len(self.ui.model.selected), 2)
            self.ui.selection_buttons[1].invoke()
            ordinary = {pid for pid, row in self.ui.model.rows.items() if row.post_kind == 'ordinary'
                        and row.disposition in {'delete_guest', 'protected_member'}}
            self.assertEqual(self.ui.model.selected, ordinary)
            self.assertEqual(len(ordinary), 4)
            picked = min(ordinary)
            self.ui.tree.focus(str(picked))
            self.ui._space(None)
            self.assertNotIn(picked, self.ui.model.selected)
            self.ui.review_button.invoke()
            self.assertIsNotNone(self.ui.review_window)
            for forbidden in (login, password, self.action):
                forbidden.assert_not_called()
        self.assertIn('조회 완료', self.ui.status.get())
        self.assertEqual([call.args for call in self.reader.call_args_list],
                         [('fixture', page) for page in range(1, 6)])

    def test_page_navigation_fetches_and_clears_old_selection(self):
        self.ui.pages.set('20')
        self.ui.query()
        self.drain()
        self.ui.select('all')
        self.ui.show_review()
        self.ui.next_page_button.invoke()
        self.assertFalse(self.ui.model.selected)
        self.assertIsNone(self.ui.review_window)
        self.drain()
        self.reader.assert_called_with('fixture', 21)
        self.ui.previous_page_button.invoke()
        self.drain()
        self.reader.assert_called_with('fixture', 20)
        self.ui.latest_button.invoke()
        self.drain()
        self.assertEqual(self.ui.pages.get(), '1')
        self.reader.assert_called_with('fixture', 1)
        self.assertIn('disabled', self.ui.previous_page_button.state())

    def test_page_navigation_boundaries_invalid_input_and_multi_page(self):
        for text, offset in [('1', -1), ('1000000', 1), ('bad', 1), ('1, 3', -1)]:
            self.ui.pages.set(text)
            self.ui.move_page(offset)
            self.assertEqual(self.ui.pages.get(), text)
        self.reader.assert_not_called()
        self.ui.pages.set('8, 10')
        self.ui.next_page_button.invoke()
        self.drain()
        self.assertEqual(self.ui.model.pages, (9, 11))
        self.assertEqual([call.args[1] for call in self.reader.call_args_list], [9, 11])
        self.ui.pages.set('bad')
        self.ui.latest_button.invoke()
        self.drain()
        self.assertEqual(self.ui.model.pages, (1,))

    def test_navigation_does_not_change_scope_during_work(self):
        self.ui.pages.set('20')
        for phase, busy in [('executing', False), ('defense', False), (None, True)]:
            self.ui.busy = busy
            self.ui._delete_state(phase)
            for button in (self.ui.latest_button, self.ui.previous_page_button, self.ui.next_page_button):
                self.assertIn('disabled', button.state())
                button.invoke()
            self.ui.refresh_latest()
            self.ui.move_page(1)
            self.assertEqual(self.ui.pages.get(), '20')
        self.ui.busy = False
        self.ui._delete_state(None)
        self.reader.assert_not_called()
        self.assertNotIn('disabled', self.ui.next_page_button.state())

    def test_scope_edit_immediately_clears_table_selection_and_review(self):
        self.ui.query()
        self.drain()
        self.ui.select('all')
        self.ui.show_review()
        self.ui.pages.set('2')
        self.assertFalse(self.ui.tree.get_children())
        self.assertFalse(self.ui.model.selected)
        self.assertIsNone(self.ui.review_window)
        self.assertIn('disabled', self.ui.review_button.state())

    def test_slow_query_is_serial_and_old_result_is_discarded(self):
        started, release = Event(), Event()
        self.addCleanup(release.set)
        def slow(*_):
            started.set()
            release.wait(2)
            return HTML
        self.reader.side_effect = slow
        self.ui.pages.set('1 2')
        self.ui.query()
        self.assertTrue(started.wait(1))
        self.ui.query()  # Busy query cannot launch another worker.
        self.ui.gallery.set('mnt')
        release.set()
        self.drain()
        self.reader.assert_called_once_with('fixture', 1)  # Cancel prevents page 2.
        self.assertFalse(self.ui.tree.get_children())
        self.assertFalse(self.ui.model.selected)
        self.assertIn('다시 조회', self.ui.status.get())

    def test_partial_failure_retains_only_read_pages_and_warns(self):
        self.reader.side_effect = [HTML, PublicReadError('synthetic failure')]
        self.ui.pages.set('1 2 3')
        self.ui.query()
        self.drain()
        self.assertEqual(self.reader.call_count, 2)
        self.assertEqual(len(self.ui.model.rows), 6)
        self.assertFalse(self.ui.model.complete)
        self.assertIn('일부 조회', self.ui.status.get())
        self.assertIn('3페이지 미조회', self.ui.detail.get())
        self.ui.select('all')
        self.assertFalse(self.ui.model.review().complete)

    def test_failed_refresh_clears_previously_selected_rows(self):
        self.ui.query()
        self.drain()
        self.ui.select('all')
        self.reader.side_effect = PublicReadError('synthetic failure')
        self.ui.query()
        self.drain()
        self.assertFalse(self.ui.model.selected)
        self.assertFalse(self.ui.tree.get_children())
        self.assertIn('조회 실패', self.ui.status.get())

    def test_unexpected_reader_exception_is_not_exposed(self):
        self.reader.side_effect = RuntimeError('private sentinel')
        self.ui.query()
        self.drain()
        self.assertIn('조회 실패', self.ui.status.get())
        self.assertNotIn('private sentinel', self.ui.status.get() + self.ui.detail.get())

    def test_invalid_input_never_reads(self):
        for gallery, pages in [('bad/gallery', '1'), ('fixture', '1 1'), ('fixture', '0')]:
            self.ui.gallery.set(gallery)
            self.ui.pages.set(pages)
            self.ui.query()
            self.assertFalse(self.ui.busy)
            self.assertFalse(self.ui.model.rows)
        self.reader.assert_not_called()

    def test_close_during_read_does_not_fetch_next_page_or_touch_destroyed_tk(self):
        started, release, finished = Event(), Event(), Event()
        self.addCleanup(release.set)
        def slow(*_):
            started.set()
            release.wait(2)
            finished.set()
            return HTML
        self.reader.side_effect = slow
        self.ui.pages.set('1 2')
        self.ui.query()
        self.assertTrue(started.wait(1))
        self.ui.close()
        release.set()
        self.assertTrue(finished.wait(1))
        self.ui.messages.get(timeout=1)  # Worker completes without a Tk callback.
        self.reader.assert_called_once()

    def test_ui_command_dispatch_is_lazy_and_does_not_login(self):
        from rectifier.__main__ import main
        with patch('rectifier.ui.run_ui', return_value=0) as run, patch('rectifier.__main__.login_check') as login:
            self.assertEqual(main(['ui']), 0)
            run.assert_called_once_with()
            login.assert_not_called()
