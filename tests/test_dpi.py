"""Offline DPI layout checks: proportional geometry and readable rows."""
from unittest import TestCase
from unittest.mock import Mock, patch
import tkinter as tk
from tkinter import font, ttk

from rectifier.dpi import pixels, size_window, enable_high_dpi, monitor_window
from rectifier.manager_ui import ManagerUI
from rectifier.defense_ui import DefenseWindow


class DpiTests(TestCase):
    def test_baseline_pixels_follow_all_display_scales(self):
        widget = Mock()
        for percent in (100, 125, 150, 175, 200, 225, 250, 300):
            with self.subTest(percent=percent):
                widget.tk.call.return_value = 96 * percent / 100 / 72
                self.assertEqual(pixels(widget, 100), percent)
                self.assertEqual(pixels(widget, (0, 20)), (0, round(percent / 5)))

    def test_window_and_minimum_fit_available_work_area(self):
        window = Mock()
        window.tk.call.return_value = 288 / 72
        with patch('rectifier.dpi.work_area', return_value=(1920, 1040)):
            size_window(window, 1120, 850, minimum=(960, 740))
        window.geometry.assert_called_once_with('1824x848')
        window.minsize.assert_called_once_with(1824, 848)

    def test_non_windows_does_not_call_windows_api(self):
        with patch('rectifier.dpi.sys.platform', 'linux'), patch('rectifier.dpi.ctypes') as api:
            enable_high_dpi()
        api.WinDLL.assert_not_called()

    def test_awareness_rejection_does_not_override_existing_manifest(self):
        with patch('rectifier.dpi.sys.platform', 'win32'), patch('rectifier.dpi.ctypes.WinDLL', create=True) as dll, patch('rectifier.dpi.ctypes.get_last_error', return_value=5, create=True):
            dll.return_value.SetProcessDpiAwarenessContext.return_value = 0
            enable_high_dpi()
        dll.return_value.SetProcessDpiAwarenessContext.assert_called_once()

    def test_main_and_dialog_layouts_at_multiple_scales(self):
        with patch('socket.socket', side_effect=AssertionError('offline DPI test')), patch('rectifier.dpi.window_dpi', return_value=None):
            for percent in (100, 125, 150, 200, 225, 250):
                with self.subTest(percent=percent):
                    root = tk.Tk()
                    root.withdraw()
                    root.tk.call('tk', 'scaling', 96 * percent / 100 / 72)
                    try:
                        with patch('rectifier.dpi.work_area', return_value=(3840, 2080)):
                            ui = ManagerUI(root)
                            root.update_idletasks()
                            row = int(ttk.Style(root).lookup(ui.tree.cget('style'), 'rowheight'))
                            line = font.Font(root, font=('맑은 고딕', 10)).metrics('linespace')
                            self.assertGreaterEqual(row, line + pixels(root, 4))
                            self.assertEqual(ui.tree.column('id', 'width'), pixels(root, 90))
                            self.assertFalse(ui.busy)
                            self.assertIsNone(ui.login_result)
                            # Main controls must fit horizontally at every scale.
                            auth = ui.account_entry.master
                            self.assertLessEqual(auth.winfo_reqwidth() + pixels(root, 40), pixels(root, 1120))
                            query = ui.gallery_box.master
                            self.assertLessEqual(query.winfo_reqwidth() + pixels(root, 40), pixels(root, 1120))
                            ui._delete_dialog('합성 결과', '본문 없는 UI 검사')
                            root.update_idletasks()
                            text = next(w for w in ui.review_window.winfo_children() if isinstance(w, tk.Text))
                            self.assertEqual(int(text.cget('padx')), pixels(root, 16))
                            ui._clear_review()
                            defense = DefenseWindow(root, 'mnt', (1,), lambda: None, task_factory=Mock())
                            root.update_idletasks()
                            self.assertIsNone(defense.task)
                            frame = defense.window.winfo_children()[0]
                            self.assertEqual(int(str(frame.cget('padding')[0])), pixels(root, 22))
                            defense.close()
                            ui.close()
                    finally:
                        try:
                            root.destroy()
                        except tk.TclError:
                            pass

    def test_live_moves_preserve_state_and_keep_windows_independent(self):
        root = tk.Tk()
        root.withdraw()
        root.tk.call('tk', 'scaling', 96 / 72)
        try:
            with patch('rectifier.dpi.window_dpi', return_value=96), patch('socket.socket', side_effect=AssertionError('offline')):
                ui = ManagerUI(root)
                ui.pages.set('1, 2')
                ui.tree.insert('', 'end', iid='sample', values=('☑', '123', '합성 글', '비회원', '일반글'))
                ui.tree.selection_set('sample')
                ui.tree.column('id', width=137)
                ui._delete_dialog('합성 결과', '유지되어야 하는 결과')
                dialog = ui.review_window
                text = next(w for w in dialog.winfo_children() if isinstance(w, tk.Text))
                main = root._rectifier_dpi
                child = dialog._rectifier_dpi
                original_style = ui.tree.cget('style')
                scaling = root.tk.call('tk', 'scaling')
                for dpi in (216, 120, 192, 96) * 3:
                    main.apply(dpi, resize=False)
                    self.assertEqual(int(ttk.Style(root).lookup(original_style, 'rowheight')), round(30 * dpi / 96))
                    self.assertEqual(ui.tree.column('id', 'width'), round(137 * dpi / 96))
                    self.assertEqual(ui.pages.get(), '1, 2')
                    self.assertEqual(ui.tree.selection(), ('sample',))
                    self.assertEqual(child.dpi, 96)
                    self.assertEqual(int(text.cget('padx')), 16)
                    self.assertEqual(root.tk.call('tk', 'scaling'), scaling)
                child.apply(240, resize=False)
                self.assertEqual(int(text.cget('padx')), 40)
                self.assertEqual(text.get('1.0', 'end').strip(), '유지되어야 하는 결과')
                self.assertEqual(main.dpi, 96)
                self.assertEqual(str(text.cget('state')), 'disabled')
                defense = DefenseWindow(root, 'mnt', (1,), lambda: None, task_factory=Mock())
                defense.include_new_members.set(True)
                defense.window._rectifier_dpi.apply(216, resize=False)
                self.assertTrue(defense.include_new_members.get())
                self.assertIsNone(defense.task)
                self.assertEqual(str(defense.start_button.cget('state')), 'normal')
                defense.task_factory.assert_not_called()
                defense.close()
                ui.close()
        finally:
            try:
                root.destroy()
            except tk.TclError:
                pass

    def test_timer_detects_dpi_and_stops_when_window_destroyed(self):
        root = tk.Tk()
        root.withdraw()
        root.tk.call('tk', 'scaling', 96 / 72)
        dialog = tk.Toplevel(root)
        label = ttk.Label(dialog, text='합성', padding=8)
        label.pack()
        try:
            with patch('rectifier.dpi.window_dpi', return_value=96):
                controller = monitor_window(dialog)
            self.assertIs(monitor_window(dialog), controller)
            dialog.after_cancel(controller.job)
            with patch('rectifier.dpi.window_dpi', return_value=216):
                controller._tick()
            self.assertEqual(controller.dpi, 216)
            self.assertEqual(int(str(label.cget('padding')[0])), 18)
            job = controller.job
            dialog.destroy()
            self.assertTrue(controller.closed)
            self.assertIsNone(controller.job)
            self.assertNotIn(job, root.tk.call('after', 'info'))
        finally:
            root.destroy()

    def test_user_window_size_scales_and_maximized_state_is_not_restored(self):
        root = tk.Tk()
        root.withdraw()
        root.tk.call('tk', 'scaling', 96 / 72)
        try:
            with patch('rectifier.dpi.window_dpi', return_value=96), patch('rectifier.dpi.work_area', return_value=(3840, 2080)):
                size_window(root, 400, 300, minimum=(300, 200))
                controller = monitor_window(root)
                with patch.object(root, 'winfo_width', return_value=640), patch.object(root, 'winfo_height', return_value=480), patch.object(root, 'geometry') as geometry:
                    controller.apply(216)
                    geometry.assert_called_once_with('1440x1080')
                with patch.object(root, 'state', return_value='zoomed'), patch.object(root, 'geometry') as geometry:
                    controller.apply(120)
                    geometry.assert_not_called()
                    self.assertEqual(root.minsize(), (375, 250))
        finally:
            root.destroy()

    def test_challenge_image_rescales_from_original_without_quality_drift(self):
        root = tk.Tk()
        root.withdraw()
        original = tk.PhotoImage(master=root, width=160, height=60)
        label = ttk.Label(root, image=original)
        label.pack()
        try:
            with patch('rectifier.dpi.window_dpi', return_value=96):
                controller = monitor_window(root, images=((label, original),))
            for dpi in (216, 120, 240, 96):
                controller.apply(dpi, resize=False)
                scaled = controller.scaled_images[0]
                self.assertEqual((scaled.width(), scaled.height()), (round(160 * dpi / 96), round(60 * dpi / 96)))
            self.assertEqual((original.width(), original.height()), (160, 60))
        finally:
            root.destroy()
