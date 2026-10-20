"""Display branding only; storage paths and credential identifiers stay stable."""
import ctypes
from ctypes import wintypes
from functools import lru_cache
from pathlib import Path
import sys
import tkinter as tk

ASSETS = Path(__file__).resolve().parent / 'assets'
# Header logo sizes rendered ahead of time: Tk can only scale photos by whole factors.
LOGO_SIZES = (48, 56, 64, 72, 80, 88, 96, 104, 112, 120, 128, 144, 160)


def logo_path(size):
    return ASSETS / 'logo' / f'logo-{size}.png'


def set_taskbar_identity():
    if sys.platform == 'win32':
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('Rectifier.Desktop')
        except (AttributeError, OSError):
            pass


def set_window_icon(window):
    window._root()._rectifier_brand_icon = True
    try:
        if sys.platform == 'win32':
            if window is not window._root():
                # The root's default icon already covers dialogs, and every Tk ico load
                # leaks about 80 GDI handles until the process exits.
                return
            window.iconbitmap(default=str(ASSETS / 'app.ico'))
            window.iconbitmap(str(ASSETS / 'app.ico'))
        else:
            picture = tk.PhotoImage(master=window, file=str(ASSETS / 'app.png'))
            window.iconphoto(True, picture)
    except (tk.TclError, OSError):
        # A missing cosmetic asset must not prevent starting or closing work.
        pass


@lru_cache(maxsize=1)
def _icon_api():
    api = ctypes.WinDLL('user32', use_last_error=True)
    api.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    api.GetAncestor.restype = wintypes.HWND
    api.GetSystemMetricsForDpi.argtypes = [ctypes.c_int, wintypes.UINT]
    api.GetSystemMetricsForDpi.restype = ctypes.c_int
    api.LoadImageW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.UINT,
                              ctypes.c_int, ctypes.c_int, wintypes.UINT]
    api.LoadImageW.restype = wintypes.HANDLE
    api.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
    api.SendMessageW.restype = ctypes.c_ssize_t
    api.DestroyIcon.argtypes = [wintypes.HANDLE]
    api.DestroyIcon.restype = wintypes.BOOL
    api.IsWindow.argtypes = [wintypes.HWND]
    api.IsWindow.restype = wintypes.BOOL
    return api


class _WindowIcons:
    """Own only our LoadImage handles; never destroy icons borrowed from Tk."""
    def __init__(self, window):
        self.window, self.handles, self.key = window, (), None
        self.dpi = 96
        window.bind('<Map>', self._mapped, add='+')
        window.bind('<Destroy>', self._destroyed, add='+')

    def _mapped(self, event):
        if event.widget is self.window:
            refresh_window_icon(self.window, self.dpi, force=True)

    def refresh(self, dpi, force=False):
        api = _icon_api()
        hwnd = api.GetAncestor(self.window.winfo_id(), 2)
        if not hwnd:
            return
        dimensions = tuple((api.GetSystemMetricsForDpi(x, dpi),
                            api.GetSystemMetricsForDpi(y, dpi))
                           for x,y in ((49,50), (11,12)))
        if any(not 1 <= value <= 512 for pair in dimensions for value in pair):
            return
        key = hwnd, dimensions
        self.dpi = dpi
        if key == self.key:
            if force:
                for kind, handle in enumerate(self.handles):
                    api.SendMessageW(hwnd, 0x80, kind, handle)
            return
        fresh = []
        for width, height in dimensions:
            handle = api.LoadImageW(None, str(ASSETS / 'app.ico'), 1, width, height, 0x10)
            if not handle:
                for loaded in fresh:
                    api.DestroyIcon(loaded)
                return
            fresh.append(handle)
        old = self.handles
        for kind, handle in enumerate(fresh):
            api.SendMessageW(hwnd, 0x80, kind, handle)
        self.handles, self.key = tuple(fresh), key
        for handle in old:
            api.DestroyIcon(handle)

    def _destroyed(self, event):
        if event.widget is not self.window:
            return
        api = _icon_api()
        if self.key and api.IsWindow(self.key[0]):
            for kind in (0,1):
                api.SendMessageW(self.key[0], 0x80, kind, 0)
        for handle in self.handles:
            api.DestroyIcon(handle)
        self.handles, self.key = (), None
        # Break the window <-> holder cycle so a root (and its Tcl interpreter) is freed now,
        # on the GUI thread, not by a cyclic GC that may run on a worker thread.
        self.window.__dict__.pop('_rectifier_icons', None)


def refresh_window_icon(window, dpi, *, force=False):
    if sys.platform != 'win32' or not getattr(window._root(), '_rectifier_brand_icon', False):
        return
    try:
        icons = getattr(window, '_rectifier_icons', None)
        if icons is None:
            _icon_api()  # Unsupported Windows retains the normal Tk icon.
            icons = window._rectifier_icons = _WindowIcons(window)
        icons.refresh(round(dpi), force=force)
    except (AttributeError, OSError, tk.TclError):
        pass
