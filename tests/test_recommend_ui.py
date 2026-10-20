import gc
from threading import Event
from time import monotonic, sleep
import tkinter as tk
from unittest import TestCase
from unittest.mock import Mock, patch

from rectifier.auth import AuthStatus, LoginResult
from rectifier.manager_ui import ManagerUI
from rectifier.page_preview import preview_pages
from rectifier.recommend_action import ReleaseReceipt
from tests.common import listing


class RecommendUITests(TestCase):
    def setUp(self):
        gc.collect()
        self.block = patch('socket.socket', side_effect=AssertionError('offline only'))
        self.block.start()
        self.root = tk.Tk()
        self.root.withdraw()
        self.action = Mock()
        self.action.execute.return_value = ReleaseReceipt(True, (101, 102))
        self.factory = Mock(return_value=self.action)
        self.ui = ManagerUI(self.root, release_factory=self.factory)
        self.session = Mock()
        self.ui.login_result = LoginResult(AuthStatus.VERIFIED, 'synthetic', self.session, 'synthetic-manager')
        self.ui._delete_state(None)
        self.ui.gallery.set('mnt')
        generation = self.ui.model.begin('mnt', (1, 2))
        self.ui.model.apply(generation, preview_pages('mnt', 'synthetic', (1, 2),
            reader=lambda _, page: listing((101, 102), kind='icon_recomimg', writer='data-uid="member"')
                if page == 1 else listing((103,))))
        self.ui._render()

    def tearDown(self):
        self.ui.close()
        self.wait(lambda: self.ui.closed)
        self.block.stop()

    def wait(self, condition):
        deadline = monotonic() + 3
        while not condition() and monotonic() < deadline:
            self.root.update()
            sleep(.01)
        self.assertTrue(condition())

    def test_selection_buttons_separate_ordinary_and_recommended(self):
        self.ui.select('all')
        self.assertEqual(self.ui.model.selected, {103})
        self.assertEqual(str(self.ui.release_button['state']), 'disabled')
        self.ui.select('recommended')
        self.assertEqual(self.ui.model.selected, {101, 102})
        self.assertEqual(str(self.ui.delete_button['state']), 'disabled')
        self.assertEqual(str(self.ui.release_button['state']), 'normal')
        self.ui._toggle('103')
        self.assertEqual(str(self.ui.release_button['state']), 'disabled')
        self.assertEqual(str(self.ui.delete_button['state']), 'disabled')

    def test_confirmation_uses_release_only_and_consumed_once(self):
        self.ui.select('recommended')
        self.ui.prepare_release()
        self.assertEqual(self.ui.delete_phase, 'recommend_ready')
        self.assertEqual(self.ui.confirm_delete_button['text'], '위 글 개념글 해제')
        self.action.execute.assert_not_called()
        self.ui.confirm_delete()  # Deletion handler cannot execute a release action.
        self.action.execute.assert_not_called()
        self.ui.confirm_release()
        self.ui.confirm_release()
        self.wait(lambda: self.ui.delete_phase is None)
        self.action.execute.assert_called_once()
        self.assertIn('해제 상태 확인 2개', self.ui.detail.get())
        self.assertFalse(self.ui.model.rows)

    def test_release_auto_requeries_viewed_pages_and_renders_remaining_rows(self):
        self.ui.reader = Mock(side_effect=lambda _, page: listing((101, 102)) if page == 1 else listing((103,)))
        self.ui.select('recommended')
        self.ui.prepare_release()
        self.ui.confirm_release()
        self.wait(lambda: self.ui.delete_phase is None)
        self.assertEqual([call.args for call in self.ui.reader.call_args_list], [('mnt', 1), ('mnt', 2)])
        self.assertEqual(set(self.ui.model.rows), {101, 102, 103})
        self.assertTrue(all(row.post_kind == 'ordinary' for row in self.ui.model.rows.values()))
        self.assertFalse(self.ui.model.selected)
        self.assertIsNotNone(self.ui.review_window)

    def test_cancel_preview_no_request_and_requires_login(self):
        self.ui.select('recommended')
        self.ui.prepare_release()
        self.ui.cancel_delete()
        self.action.execute.assert_not_called()
        self.assertIsNone(self.ui.delete_phase)
        self.ui.login_result = None
        with patch('rectifier.manager_ui.messagebox.showwarning') as warning:
            self.ui.prepare_release()
            warning.assert_called_once()
        self.factory.assert_called_once()

    def test_scope_change_clears_selection_and_disable_actions(self):
        self.ui.select('recommended')
        self.ui.pages.set('3')
        self.assertFalse(self.ui.model.selected)
        self.assertEqual(str(self.ui.release_button['state']), 'disabled')

    def test_release_controls_fit_multiple_display_scales(self):
        from rectifier.dpi import pixels
        for percent in (100, 150, 225, 250):
            with self.subTest(percent=percent), patch('rectifier.dpi.window_dpi', return_value=None), \
                    patch('rectifier.dpi.work_area', return_value=(3840, 2080)):
                root = tk.Tk()
                root.withdraw()
                root.tk.call('tk', 'scaling', 96 * percent / 100 / 72)
                ui = ManagerUI(root)
                try:
                    root.update_idletasks()
                    self.assertLessEqual(ui.release_button.master.winfo_reqwidth(), pixels(root, 920))
                finally:
                    ui.close()

    def test_close_waits_for_running_release_before_closing_session(self):
        entered, finish = Event(), Event()
        def execute(cancel):
            entered.set()
            finish.wait(2)
            return ReleaseReceipt(True, unknown=(101, 102), stopped='user_stopped')
        self.action.execute.side_effect = execute
        self.ui.select('recommended')
        self.ui.prepare_release()
        self.ui.confirm_release()
        self.assertTrue(entered.wait(1))
        self.ui.close()
        self.assertFalse(self.ui.closed)
        self.assertTrue(self.ui.delete_cancel.is_set())
        self.session.close.assert_not_called()
        finish.set()
        self.wait(lambda: self.ui.closed)
        self.session.close.assert_called_once()
