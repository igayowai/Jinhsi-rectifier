"""Local GUI smoke test with a synthetic GIF and synthetic answer. No network."""
import socket
import tkinter as tk
from tkinter import ttk
from unittest.mock import patch
from rectifier.captcha_ui import prompt_captcha
from tests.test_captcha import GIF


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


def run(confirm):
    root = tk.Tk()
    def interact():
        entry = next(w for w in descendants(root) if isinstance(w, ttk.Entry))
        entry.insert(0, 'Ab1234')
        label = '확인' if confirm else '취소'
        next(w for w in descendants(root) if isinstance(w, ttk.Button) and w.cget('text') == label).invoke()
    root.after(250, interact)
    with patch('tkinter.Tk', return_value=root), patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')):
        answer = prompt_captcha(GIF)
    assert answer == ('Ab1234' if confirm else None)


if __name__ == '__main__':
    run(True)
    run(False)
    print('GUI submit/cancel: PASS; network requests: 0')
