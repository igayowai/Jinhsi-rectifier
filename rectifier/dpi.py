"""Per-window DPI layout for Tk 8.6, without mutating shared Tk font/scaling state."""
import ctypes
from ctypes import wintypes
import sys
from functools import lru_cache


def enable_high_dpi():
    """Request Per-Monitor V2 before Tk; respect an existing manifest policy."""
    if sys.platform != 'win32':
        return
    try:
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        setter = user32.SetProcessDpiAwarenessContext
        setter.argtypes = [ctypes.c_void_p]
        setter.restype = wintypes.BOOL
        if not setter(ctypes.c_void_p(-4)) and ctypes.get_last_error() == 87:
            setter(ctypes.c_void_p(-3))  # Older Windows 10: Per-Monitor V1.
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def tk_dpi(widget):
    return round(float(widget.tk.call('tk', 'scaling')) * 72)


def pixels(widget, value):
    """Convert baseline pixels; character widths and positive font sizes stay put."""
    # Tk derives scaling from integer screen millimetres; round back to DPI to
    # avoid 100% becoming 99.9% and accumulating a one-pixel drift in wide rows.
    controller = getattr(widget.winfo_toplevel(), '_rectifier_dpi', None)
    dpi = controller.dpi if isinstance(controller, WindowDpi) else tk_dpi(widget)
    scale = dpi / 96
    if isinstance(value, (tuple, list)):
        return tuple(round(item * scale) for item in value)
    return round(value * scale)


def apply_theme(window):
    """Apply sv-ttk light theme with Malgun Gothic fonts; fall back cleanly to default ttk."""
    import tkinter as tk
    from tkinter import font, ttk
    root = window._root()
    if not getattr(root, '_rectifier_theme_loaded', False):
        root._rectifier_theme_loaded = True
        try:
            import sv_ttk
            sv_ttk.set_theme('light', root=root)
        except Exception:
            pass
    style = ttk.Style(window)
    for name, size, weight in (
        ('SunValleyCaptionFont', 9, 'normal'),
        ('SunValleyBodyFont', 10, 'normal'),
        ('SunValleyBodyStrongFont', 10, 'bold'),
        ('SunValleyBodyLargeFont', 12, 'normal'),
        ('SunValleySubtitleFont', 13, 'bold'),
        ('SunValleyTitleFont', 16, 'bold'),
        ('SunValleyTitleLargeFont', 20, 'bold'),
        ('SunValleyDisplayFont', 28, 'bold'),
    ):
        try:
            f = font.Font(root=root, name=name, exists=True)
            f.configure(family='맑은 고딕', size=size, weight=weight)
        except tk.TclError:
            pass
    try:
        if style.theme_use() in ('sun-valley-light', 'sun-valley-dark'):
            root.tk.call('configure_colors')
    except tk.TclError:
        pass
    style.configure('.', font=('맑은 고딕', 10))
    style.configure('TNotebook.Tab', font=('맑은 고딕', 10, 'bold'))
    style.configure('TLabelframe.Label', font=('맑은 고딕', 10, 'bold'))
    style.configure('Treeview', rowheight=pixels(window, 30), font=('맑은 고딕', 10))
    style.configure('Treeview.Heading', font=('맑은 고딕', 10, 'bold'))


class MonitorInfo(ctypes.Structure):
    _fields_ = [('cbSize', wintypes.DWORD), ('rcMonitor', wintypes.RECT),
                ('rcWork', wintypes.RECT), ('dwFlags', wintypes.DWORD)]


@lru_cache(maxsize=1)
def _user32():
    api = ctypes.WinDLL('user32', use_last_error=True)
    api.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    api.GetAncestor.restype = wintypes.HWND
    api.GetDpiForWindow.argtypes = [wintypes.HWND]
    api.GetDpiForWindow.restype = wintypes.UINT
    api.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    api.MonitorFromWindow.restype = wintypes.HANDLE
    api.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
    api.GetMonitorInfoW.restype = wintypes.BOOL
    return api


def _hwnd(widget):
    # Tk exposes its client HWND; the outer wrapper owns monitor/DPI state.
    handle = widget.winfo_toplevel().winfo_id()
    return _user32().GetAncestor(handle, 2) or handle


def window_dpi(widget):
    if sys.platform == 'win32':
        try:
            return _user32().GetDpiForWindow(_hwnd(widget)) or None
        except (AttributeError, OSError):
            pass
    return None


def work_area(widget):
    """Work area of this window's monitor, including secondary/negative monitors."""
    if sys.platform == 'win32':
        try:
            api = _user32()
            monitor = api.MonitorFromWindow(_hwnd(widget), 2)
            info = MonitorInfo(cbSize=ctypes.sizeof(MonitorInfo))
            if api.GetMonitorInfoW(monitor, ctypes.byref(info)):
                rect = info.rcWork
                return rect.right - rect.left, rect.bottom - rect.top
        except (AttributeError, OSError):
            pass
    return widget.winfo_screenwidth(), widget.winfo_screenheight()


def work_origin(widget):
    """Top-left of this window's monitor work area, or None when it cannot be read."""
    if sys.platform == 'win32':
        try:
            api = _user32()
            monitor = api.MonitorFromWindow(_hwnd(widget), 2)
            info = MonitorInfo(cbSize=ctypes.sizeof(MonitorInfo))
            if api.GetMonitorInfoW(monitor, ctypes.byref(info)):
                return info.rcWork.left, info.rcWork.top
        except (AttributeError, OSError):
            pass
    return None


def size_window(window, width, height, *, minimum=None):
    """Scale geometry and keep the client plus title bar inside the work area."""
    window._rectifier_size = (width, height, minimum)
    area_w, area_h = work_area(window)
    available = (max(1, area_w - pixels(window, 32)),
                 max(1, area_h - pixels(window, 64)))
    size = tuple(min(pixels(window, value), bound) for value, bound in zip((width, height), available))
    position = ''
    origin = None if getattr(window, '_rectifier_placed', False) else work_origin(window)
    window._rectifier_placed = True
    if origin is not None:
        # First placement only: centre inside the work area so the bottom row of
        # buttons never starts under the taskbar. Later resizes keep the user's position.
        x = origin[0] + max(0, (area_w - size[0]) // 2)
        y = origin[1] + max(0, (area_h - size[1] - pixels(window, 40)) // 2)
        position = f'+{x}+{y}'
    window.geometry(f'{size[0]}x{size[1]}{position}')
    if minimum:
        window.minsize(*(min(pixels(window, value), bound) for value, bound in zip(minimum, size)))


class WindowDpi:
    """Keep each toplevel's fonts/styles independent; rescale existing widgets.

    A bounded Tk timer also catches OS display-setting changes without a move.
    No native WndProc replacement: callbacks execute only in Tk's GUI thread.
    No widgets, text, selections, commands or network workers are recreated.
    """
    def __init__(self, window, *, base_dpi=None, images=()):
        import tkinter as tk
        from tkinter import font, ttk
        self.window = window
        self.dpi = base_dpi or tk_dpi(window)
        self.base = self.dpi
        self.style = ttk.Style(window)
        self.fonts, self.records, self.styles = {}, [], {}
        self.images, self.scaled_images = tuple(images), []
        self.job = None
        self.closed = False
        self.font_base = tk_dpi(window)
        counter = getattr(window._root(), '_dpi_style_counter', 0) + 1
        window._root()._dpi_style_counter = counter
        self.prefix = f'RectifierDpi{counter}'
        self._font_type = font.Font
        self._tk = tk
        self._ttk = ttk
        self._capture(window)
        window._rectifier_dpi = self
        window.bind('<Destroy>', self._destroyed, add='+')
        # Initial layout was built using base_dpi; target may be another monitor.
        self.apply(window_dpi(window) or self.dpi, initial=True)
        self.job = window.after(200, self._tick)

    def _dimensions(self, widget, value):
        values = widget.tk.splitlist(value) if not isinstance(value, (int, float)) else (value,)
        return tuple(widget.winfo_pixels(v) * 96 / self.base for v in values)

    def _font(self, specification):
        key = str(specification or 'TkDefaultFont')
        if key not in self.fonts:
            original = self._font_type(root=self.window, font=specification or 'TkDefaultFont')
            attributes = original.actual()
            size = original.cget('size')
            points = size if size > 0 else -size * 72 / self.font_base
            local = self._font_type(root=self.window, **attributes)
            self.fonts[key] = (local, points)
        return self.fonts[key][0]

    def _local_style(self, base):
        if base in self.styles:
            return self.styles[base][0]
        name = f'{self.prefix}.{base}'
        config = {'font': self._font(self.style.lookup(base, 'font'))}
        dimensions = {}
        for option in ('padding', 'rowheight', 'arrowsize', 'borderwidth'):
            value = self.style.lookup(base, option)
            if value != '':
                dimensions[option] = self._dimensions(self.window, value)
        self.styles[base] = (name, dimensions)
        self.style.configure(name, **config)
        return name

    def _capture(self, parent):
        for widget in parent.winfo_children():
            if isinstance(widget, self._tk.Toplevel):
                continue  # Another monitor may own that independent window.
            options = {}
            for key in ('padding', 'padx', 'pady', 'wraplength', 'borderwidth',
                        'highlightthickness', 'insertwidth', 'spacing1', 'spacing2', 'spacing3'):
                if key in widget.keys():
                    value = widget.cget(key)
                    if str(value):
                        options[key] = self._dimensions(widget, value)
            manager = widget.winfo_manager()
            layout = {}
            if manager in ('grid', 'pack'):
                info = getattr(widget, manager + '_info')()
                layout = {key: self._dimensions(widget, info[key])
                          for key in ('padx', 'pady', 'ipadx', 'ipady') if key in info}
            if 'font' in widget.keys():
                specification = widget.cget('font')
                if not str(specification) and isinstance(widget, self._ttk.Widget):
                    specification = self.style.lookup(widget.cget('style') or widget.winfo_class(), 'font')
                widget.configure(font=self._font(specification))
            columns = {}
            if isinstance(widget, self._ttk.Widget):
                base = widget.cget('style') or widget.winfo_class()
                if isinstance(widget, (self._ttk.Scrollbar, self._ttk.Scale, self._ttk.Progressbar)):
                    base = str(widget.cget('orient')).title() + '.' + base
                local = self._local_style(base)
                widget.configure(style=local)
                if isinstance(widget, self._ttk.Notebook):
                    self._local_style(base + '.Tab')
                elif isinstance(widget, self._ttk.LabelFrame):
                    self._local_style(base + '.Label')
                elif isinstance(widget, self._ttk.Treeview):
                    self._local_style(base + '.Heading')
                    for col in ('#0', *widget['columns']):
                        columns[col] = {key: widget.column(col, key) * 96 / self.base
                                        for key in ('width', 'minwidth')}
            self.records.append((widget, options, manager, layout, columns))
            self._capture(widget)

    @staticmethod
    def _scaled(values, dpi):
        result = tuple(round(value * dpi / 96) for value in values)
        return result[0] if len(result) == 1 else result

    def apply(self, dpi, *, initial=False, resize=True):
        """Also callable with synthetic DPI in offline tests, without OS changes."""
        if self.closed or (dpi == self.dpi and not initial):
            return
        if not 48 <= dpi <= 768:
            return
        old = self.dpi
        width, height = self.window.winfo_width(), self.window.winfo_height()
        self.dpi = dpi
        if isinstance(self.window, (self._tk.Tk, self._tk.Toplevel)):
            from .branding import refresh_window_icon
            refresh_window_icon(self.window, dpi)
        for local, points in self.fonts.values():
            local.configure(size=-max(1, round(points * dpi / 72)))
        for name, dimensions in self.styles.values():
            self.style.configure(name, **{key: self._scaled(value, dpi) for key, value in dimensions.items()})
        for widget, options, manager, layout, columns in self.records:
            if not widget.winfo_exists():
                continue
            widget.configure(**{key: self._scaled(value, dpi) for key, value in options.items()})
            if layout:
                getattr(widget, manager + '_configure')(**{key: self._scaled(value, dpi) for key, value in layout.items()})
            for col, values in columns.items():
                if not initial:
                    # Preserve user resizing and Treeview's stretched widths.
                    actual = widget.column(col, 'width')
                    if actual != round(values['width'] * old / 96):
                        values['width'] = actual * 96 / old
                widget.column(col, **{key: round(value * dpi / 96) for key, value in values.items()})
        if self.images:
            from fractions import Fraction
            ratio = Fraction(dpi, 96).limit_denominator(8)
            self.scaled_images = []
            for label, original in self.images:
                scaled = original.zoom(ratio.numerator).subsample(ratio.denominator)
                label.configure(image=scaled)
                self.scaled_images.append(scaled)
        size = getattr(self.window, '_rectifier_size', None)
        if resize and size and size[2] and self.window.state() in ('zoomed', 'iconic'):
            # A restored window must not retain the previous monitor's minimum.
            area_w, area_h = work_area(self.window)
            bounds = (max(1, area_w - pixels(self.window, 32)),
                      max(1, area_h - pixels(self.window, 64)))
            self.window.minsize(*(min(pixels(self.window, value), bound)
                                  for value, bound in zip(size[2], bounds)))
        if resize and size and self.window.state() not in ('zoomed', 'iconic'):
            w, h, minimum = size
            if not initial and width > 1 and height > 1:
                w, h = width * 96 / old, height * 96 / old
            size_window(self.window, w, h, minimum=minimum)

    def _tick(self):
        self.job = None
        if self.closed:
            return
        try:
            self.apply(window_dpi(self.window) or self.dpi)
        except self._tk.TclError:
            if not self.window.winfo_exists():
                return
            raise
        self.job = self.window.after(200, self._tick)

    def _destroyed(self, event):
        if event.widget is self.window:
            self.closed = True
            if self.job:
                try:
                    self.window.after_cancel(self.job)
                except self._tk.TclError:
                    pass
                self.job = None
            self.fonts.clear()
            self.records.clear()
            self.images = ()
            self.scaled_images.clear()
            self.style = None
            # Break the window <-> controller cycle so the window (and its fonts) are freed now,
            # on the GUI thread, not later by a cyclic GC that may run on a worker thread.
            self.window.__dict__.pop('_rectifier_dpi', None)


def monitor_window(window, *, base_dpi=None, images=()):
    existing = getattr(window, '_rectifier_dpi', None)
    if isinstance(existing, WindowDpi):
        return existing
    return WindowDpi(window, base_dpi=base_dpi, images=images)
