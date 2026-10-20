"""A local Tk dialog displaying only the challenge image, never the login HTML."""
import base64
from . import APP_NAME
from .branding import set_taskbar_identity, set_window_icon
from .captcha import CaptchaError, validate_answer, validate_image
from .dpi import enable_high_dpi, pixels, monitor_window
from .diagnostics import record_error


def prompt_captcha(image_bytes, *, parent=None, cancelled=None):
    validate_image(image_bytes)
    import tkinter as tk
    from tkinter import ttk
    root = None
    answer = None
    try:
        if parent is None:
            set_taskbar_identity()
            enable_high_dpi()
        root = tk.Tk() if parent is None else tk.Toplevel(parent)
        px = lambda value: pixels(root, value)
        root.title(f'{APP_NAME} — 디시 보안문자 입력')
        set_window_icon(root)
        root.resizable(False, False)
        frame = ttk.Frame(root, padding=px(20))
        frame.pack()
        ttk.Label(frame, text='이미지에 보이는 보안문자를 입력한 뒤 확인을 누르세요.').pack(pady=px((0, 12)))
        picture = tk.PhotoImage(master=root, data=base64.b64encode(image_bytes).decode('ascii'))
        image_label = ttk.Label(frame, image=picture)
        image_label.pack()
        entry = ttk.Entry(frame, width=24, justify='center')
        entry.pack(pady=px(12))
        notice = ttk.Label(frame, text='5분 안에 입력해야 함. 닫으면 로그인을 중지함.')
        notice.pack()

        def close():
            entry.delete(0, 'end')
            if parent is None:
                root.quit()
            else:
                root.destroy()

        def submit(event=None):
            nonlocal answer
            try:
                answer = validate_answer(entry.get())
            except CaptchaError:
                notice.config(text='보안문자를 입력해야 함. 영문·숫자만 입력 가능함.')
                return
            close()

        buttons = ttk.Frame(frame)
        buttons.pack(pady=px((12, 0)))
        ttk.Button(buttons, text='확인', command=submit).pack(side='left', padx=px(6))
        ttk.Button(buttons, text='취소', command=close).pack(side='left', padx=px(6))
        root.protocol('WM_DELETE_WINDOW', close)
        root.bind('<Return>', submit)
        root.bind('<Escape>', lambda event: close())
        root.after(300_000, close)
        def check_cancel():
            if cancelled is not None and cancelled():
                close()
            elif root.winfo_exists():
                root.after(100, check_cancel)
        if cancelled is not None:
            root.after(100, check_cancel)
        monitor_window(root, images=((image_label, picture),))
        root.lift()
        entry.focus_force()
        if parent is None:
            root.mainloop()
        else:
            root.transient(parent)
            root.grab_set()
            parent.wait_window(root)
            if parent.winfo_exists():
                parent.grab_set()
        return answer
    except Exception:
        record_error('captcha_ui.prompt_captcha')
        raise CaptchaError('보안문자 입력창을 열거나 표시하지 못함') from None
    finally:
        image_bytes = b''
        if root is not None and root.winfo_exists():
            root.destroy()
