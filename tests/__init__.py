# The offline suite must never append to the user's real diagnostics folder.
import gc
import tkinter

import rectifier.diagnostics

rectifier.diagnostics.ENABLED = False

# Finished UI tests leave their Tk roots in reference cycles (test case <-> widgets). If a cyclic
# GC later frees one on a worker thread, Tcl crashes the process. Free them on the main thread
# before each new root instead.
_tk_init = tkinter.Tk.__init__


def _collect_then_init(self, *args, **kwargs):
    gc.collect()
    _tk_init(self, *args, **kwargs)


tkinter.Tk.__init__ = _collect_then_init

# Tk never frees an .ico it loads (about 80 GDI handles each). The app loads it once per run;
# the suite creates a root per test and would hit Windows' 10,000-handle limit and crash.
# Native icon sizes are set separately (branding._WindowIcons) and stay under test.
_iconbitmap = tkinter.Wm.wm_iconbitmap


def _skip_ico_file(self, bitmap=None, default=None):
    if str(bitmap or default or '').lower().endswith('.ico'):
        return ''
    return _iconbitmap(self, bitmap, default)


tkinter.Wm.wm_iconbitmap = tkinter.Wm.iconbitmap = _skip_ico_file
