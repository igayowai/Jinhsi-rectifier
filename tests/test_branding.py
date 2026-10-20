"""Native icon sizes follow each window's DPI, including non-preset scaling."""
import ctypes as c
from ctypes import wintypes as w
from pathlib import Path
import sys
import tkinter as tk
from unittest import TestCase, skipUnless
from unittest.mock import patch

from rectifier.branding import set_window_icon, refresh_window_icon, _icon_api
from rectifier.dpi import monitor_window


class IconInfo(c.Structure):
    _fields_ = [('icon',w.BOOL),('x',w.DWORD),('y',w.DWORD),('mask',w.HANDLE),('color',w.HANDLE)]


class Bitmap(c.Structure):
    _fields_ = [('type',w.LONG),('width',w.LONG),('height',w.LONG),('stride',w.LONG),
                ('planes',w.WORD),('depth',w.WORD),('bits',c.c_void_p)]


@skipUnless(sys.platform == 'win32', 'native Windows icon verification')
class BrandingTests(TestCase):
    def setUp(self):
        self.root = tk.Tk()
        set_window_icon(self.root)
        self.controller = monitor_window(self.root)
        self.root.update()
        self.api = _icon_api()
        self.api.GetIconInfo.argtypes = [w.HANDLE, c.POINTER(IconInfo)]
        self.api.GetIconInfo.restype = w.BOOL
        self.gdi = c.WinDLL('gdi32')
        self.gdi.GetObjectW.argtypes = [w.HANDLE,c.c_int,c.c_void_p]
        self.gdi.GetObjectW.restype = c.c_int
        self.gdi.DeleteObject.argtypes = [w.HANDLE]

    def tearDown(self):
        self.root.destroy()

    def sizes(self, window):
        hwnd = self.api.GetAncestor(window.winfo_id(), 2)
        result = []
        for kind in (0,1):
            handle = self.api.SendMessageW(hwnd, 0x7f, kind, 0)
            info, bitmap = IconInfo(), Bitmap()
            self.assertTrue(self.api.GetIconInfo(handle, c.byref(info)))
            try:
                self.assertTrue(self.gdi.GetObjectW(info.color,c.sizeof(bitmap),c.byref(bitmap)))
                result.append((bitmap.width,bitmap.height))
            finally:
                self.gdi.DeleteObject(info.color)
                self.gdi.DeleteObject(info.mask)
        return tuple(result)

    def expected(self, dpi):
        return tuple((self.api.GetSystemMetricsForDpi(x,dpi),self.api.GetSystemMetricsForDpi(y,dpi))
                     for x,y in ((49,50),(11,12)))

    def test_native_dimensions_from_100_to_800_percent_and_custom_dpi(self):
        for dpi in (*range(96,769,24),137,181,241):
            with self.subTest(dpi=dpi):
                self.controller.apply(dpi, resize=False)
                self.assertEqual(self.sizes(self.root),self.expected(dpi))

    def test_windows_keep_independent_sizes_and_remap_retains_icons(self):
        child = tk.Toplevel(self.root)
        controller = monitor_window(child)
        child.update()
        self.controller.apply(96,resize=False)
        controller.apply(216,resize=False)
        self.assertEqual(self.sizes(self.root), self.expected(96))
        self.assertEqual(self.sizes(child), self.expected(216))
        child.withdraw()
        child.deiconify()
        child.update()
        self.assertEqual(self.sizes(child), self.expected(216))
        icons = child._rectifier_icons
        owned = icons.handles
        child.destroy()
        self.assertEqual(icons.handles,())
        for handle in owned:
            self.assertFalse(self.api.GetIconInfo(handle,c.byref(IconInfo())))

    def test_missing_asset_retains_current_icons(self):
        old = self.root._rectifier_icons.handles
        dimensions = self.sizes(self.root)
        with patch('rectifier.branding.ASSETS',Path('missing-icon-directory')):
            refresh_window_icon(self.root,144)
        self.assertEqual(self.root._rectifier_icons.handles,old)
        self.assertEqual(self.sizes(self.root),dimensions)

    def test_repeated_same_dpi_reuses_handles(self):
        old = self.root._rectifier_icons.handles
        for _ in range(15):
            refresh_window_icon(self.root,self.controller.dpi,force=True)
        self.assertEqual(self.root._rectifier_icons.handles,old)
