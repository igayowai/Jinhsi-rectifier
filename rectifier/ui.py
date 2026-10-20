"""Desktop entry point. The window itself lives in manager_ui."""
import tkinter as tk

from .branding import set_taskbar_identity
from .dpi import enable_high_dpi


def run_ui():
    from .manager_ui import ManagerUI
    from .account_profiles import AccountProfiles, GalleryStore
    set_taskbar_identity()
    enable_high_dpi()
    root = tk.Tk()
    ManagerUI(root, profile_store=AccountProfiles(), gallery_store=GalleryStore())
    root.mainloop()
    return 0
