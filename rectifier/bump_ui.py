"""Bump window. Standalone it signs in by itself; opened from the main window it borrows that session."""
from queue import Empty
from math import ceil, isfinite
import tkinter as tk
from tkinter import messagebox, ttk

from .bump import BumpLimits, BumpTarget, validate_bump_targets
from .bump_task import BumpTask
from .account_task import LoginTask
from .account_profiles import AccountProfiles, close_login, disable_auto_login, remember_after_login
from .paths import DATA_ROOT
from .captcha_ui import prompt_captcha
from .credentials import CredentialError, _identity, forget_password, save_password_new
from .dpi import apply_theme, monitor_window, pixels, size_window
from .diagnostics import record_error


REASONS = {
    'request_limit': '지정한 요청 횟수 완료', 'duration_limit': '지정한 실행 시간 종료',
    'user_stopped': '사용자 중지',
    'auth_required': '로그인이 풀림 · 다시 로그인한 뒤 시작하세요',
    'credential_missing': '저장된 비밀번호 없음 · 비밀번호를 입력하고 다시 시작할 수 있음',
    'forbidden': '이 갤러리의 끌올 권한이 없음 · 갤러리와 계정을 확인하세요',
    'rate_limited': '서버가 요청을 잠시 제한함 · 몇 분 뒤 간격을 늘려 다시 시작하세요',
    'invalid_listing': '목록에서 글을 찾지 못함 · 공지·고정글이거나 주소가 틀렸을 수 있음',
    'ineligible_target': '끌올할 수 없는 글 종류임 · 다른 글 주소를 넣으세요',
    'target_unverified': '지정한 글을 확인하지 못함 · 글 주소와 삭제 여부를 확인하세요',
    'target_changed': '실행 중 글 제목·작성자가 바뀜 · 주소를 다시 확인하고 시작하세요',
    'read_failed': '인터넷 연결이 불안정함 · 연결 확인 후 다시 시작하세요',
    'stale_check': '권한·글 확인이 늦어져 안전하게 멈춤 · 다시 시작하면 됨',
    'response_unknown': '처리 결과 미확정 · 갤러리 관리 내역에서 끌올 기록을 확인하세요',
    'rejection_reported': '서버가 요청을 거부함 · 끌올 권한과 글 상태를 확인하세요',
    'worker_failed': '예상하지 못한 오류로 멈춤 · 다시 시작해 보고, 반복되면 진단 로그를 확인하세요',
}

SINGLE_HELP = '시작하면 바로 1회 끌올하고 정한 간격으로 반복 · 요청마다 0~0.2초 추가 대기 · 통신이 느리면 더 느려짐\n'
MULTI_HELP = '등록한 순서대로 돌아가며 반복 · 오류가 나면 모든 글이 멈춤 · 통신이 느리면 간격이 늘어남\n'
RUN_HELP = (
    '성공 응답은 서버 메시지 기준이며 순위 유지를 보장하지 않음 · 중지해도 이미 보낸 요청은 되돌릴 수 없음\n'
    '결과 미확정이면 갤러리 관리 내역에서 끌올 기록을 확인한 뒤 다시 시험 · 권한·제한 오류 시 멈춘 채로 둠'
)

PERIODS = {'1분마다': 60, '2분마다': 120, '5분마다': 300,
           '10분마다': 600, '20분마다': 1200, '30분마다': 1800}


class BumpWindow:
    def __init__(self, parent, on_closed=lambda: None, *, account='', target_url='', task_factory=BumpTask,
                 password_saver=save_password_new, password_remover=forget_password,
                 confirm_forget=messagebox.askyesno, profile_store=None, login_task_factory=LoginTask,
                 login_result=None, embedded=False, before_start=None, on_started=None, on_stopped=None):
        apply_theme(parent)
        self.on_closed, self.task_factory = on_closed, task_factory
        self.password_saver, self.password_remover = password_saver, password_remover
        self.confirm_forget = confirm_forget
        self.profile_store, self.login_task_factory = profile_store, login_task_factory
        self.embedded = embedded
        self.before_start = before_start
        self.on_started = on_started
        self.on_stopped = on_stopped
        self.login_task = None
        # A borrowed session belongs to the main window: never closed, replaced or saved here.
        self.login_result = login_result
        self._borrowed = login_result is not None or embedded
        if self.login_result is not None:
            account, self.profile_store = login_result.verified_user_id, None
        elif embedded:
            self.profile_store = None
        self.auto_login_id = self._pending_start = None
        self._automatic_attempt = False
        self.task = None
        self.experimental = False
        self._finished_reported = False
        self.closed = self.close_requested = False
        if embedded:
            self.window = ttk.Frame(parent)
            self.window.pack(fill='both', expand=True)
            self.window.withdraw = lambda: None
            self.window.deiconify = lambda: None
            self.window.title = lambda *args: ''
            self.window.protocol = lambda *args: None
            self.window.transient = lambda *args: None
            self.window.grab_set = lambda *args: None
            self.toplevel = parent.winfo_toplevel()
        else:
            self.window = tk.Toplevel(parent)
            self.window.title('정류기 — 지정 글 끌올')
            size_window(self.window, 850, 760, minimum=(800, 730))
            self.window.protocol('WM_DELETE_WINDOW', self.close)
            self.toplevel = self.window
        px = lambda value: pixels(self.window, value)
        self.mode_var = tk.BooleanVar(self.window, False)
        self.url = tk.StringVar(self.window, target_url)
        self.second_url = tk.StringVar(self.window, '')
        self.account = tk.StringVar(self.window, account)
        self.password = tk.StringVar(self.window, '')
        self.credential_status = tk.StringVar(self.window, '')
        self.login_status = tk.StringVar(self.window, '로그인 대기')
        self.auto_login = tk.BooleanVar(self.window, False)
        self.minutes = tk.StringVar(self.window, '1')
        self.rate = tk.StringVar(self.window, '2')
        self.period = tk.StringVar(self.window, '초당 횟수')
        self.status = tk.StringVar(self.window, '대기 · 글 주소와 관리 계정을 입력한 뒤 1회 시험 가능')
        self.counts = tk.StringVar(self.window, '요청 0회 · 성공 응답 0회 · 결과 미확정 0회')
        self.target_label = tk.StringVar(self.window, '대상: 아직 지정하지 않음')
        frame = ttk.Frame(self.window, padding=px(22))
        frame.pack(fill='both', expand=True)
        # Keep start/stop reachable even with a long target list or short display.
        buttons = ttk.Frame(frame)
        buttons.pack(side='bottom', fill='x', pady=px(6))
        viewport = ttk.Frame(frame)
        viewport.pack(fill='both', expand=True)
        self.body_canvas = tk.Canvas(viewport, highlightthickness=0, borderwidth=0,
                                     background=ttk.Style(self.window).lookup('TFrame', 'background'))
        scrollbar = ttk.Scrollbar(viewport, orient='vertical', command=self.body_canvas.yview)
        scrollbar.pack(side='right', fill='y')
        self.body_canvas.configure(yscrollcommand=scrollbar.set)
        self.body_canvas.pack(side='left', fill='both', expand=True)
        frame = ttk.Frame(self.body_canvas)
        body = self.body_canvas.create_window((0, 0), window=frame, anchor='nw')
        self.body_canvas.bind('<Configure>', lambda e: self.body_canvas.itemconfigure(body, width=e.width))
        frame.bind('<Configure>', lambda e: self.body_canvas.configure(scrollregion=self.body_canvas.bbox('all')))
        header_row = ttk.Frame(frame)
        header_row.pack(fill='x')
        ttk.Label(header_row, text='지정 글 끌올', font=('맑은 고딕', 18 if embedded else 20, 'bold')).pack(side='left')
        mode_box = ttk.Frame(header_row)
        mode_box.pack(side='right')
        self.single_mode_radio = ttk.Radiobutton(
            mode_box, text='단일 글 끌올', value=False, variable=self.mode_var,
            command=lambda: self.set_experimental(False))
        self.single_mode_radio.pack(side='left', padx=px((0, 10)))
        self.multi_mode_radio = ttk.Radiobutton(
            mode_box, text='여러 글 순환 끌올 (2~20개)', value=True, variable=self.mode_var,
            command=lambda: self.set_experimental(True))
        self.multi_mode_radio.pack(side='left')
        self.url_label = ttk.Label(frame, text='마이너 갤러리 글 주소')
        self.url_label.pack(anchor='w', pady=px((10, 4)))
        self.url_entry = ttk.Entry(frame, textvariable=self.url)
        self.url_entry.pack(fill='x')
        self.experiment_box = ttk.Frame(frame)
        ttk.Label(self.experiment_box, text='2번 글 주소 · 같은 갤러리의 다른 글').pack(anchor='w', pady=px((8, 4)))
        self.second_url_entry = ttk.Entry(self.experiment_box, textvariable=self.second_url)
        self.second_url_entry.pack(fill='x')
        ttk.Label(self.experiment_box, text='3번 이후 글 주소 · 한 줄에 하나씩 · 전체 최대 20개').pack(anchor='w', pady=px((8, 4)))
        extra_box = ttk.Frame(self.experiment_box)
        extra_box.pack(fill='x')
        self.additional_urls = tk.Text(extra_box, height=3, width=40, wrap='none')
        extra_scroll = ttk.Scrollbar(extra_box, orient='vertical', command=self.additional_urls.yview)
        extra_scroll.pack(side='right', fill='y')
        self.additional_urls.configure(yscrollcommand=extra_scroll.set)
        self.additional_urls.pack(side='left', fill='x', expand=True)
        ttk.Label(self.experiment_box, wraplength=px(710), text=(
            '1 → 2 → 3 → … → 1 순환 · 초당 횟수는 모든 글의 합계 · 기본 초당 1회'
        )).pack(anchor='w', pady=px((6, 0)))
        results_box = ttk.Frame(self.experiment_box)
        results_box.pack(fill='x', pady=px((6, 0)))
        self.target_rows = ttk.Treeview(results_box, columns=('post', 'title', 'count'), show='headings', height=3)
        for name, heading, width in (('post', '순서 / 글번호', 135), ('title', '제목', 400), ('count', '성공 응답', 85)):
            self.target_rows.heading(name, text=heading)
            self.target_rows.column(name, width=px(width), minwidth=px(50), stretch=name == 'title')
        rows_scroll = ttk.Scrollbar(results_box, orient='vertical', command=self.target_rows.yview)
        rows_scroll.pack(side='right', fill='y')
        self.target_rows.configure(yscrollcommand=rows_scroll.set)
        self.target_rows.pack(side='left', fill='x', expand=True)
        login_box = self.login_box = ttk.Frame(frame)
        login_box.pack(fill='x', pady=px(10))
        login_box.columnconfigure(1, weight=1)
        login_box.columnconfigure(3, weight=1)
        ttk.Label(login_box, text='로그인 ID').grid(row=0, column=0, padx=px((0, 8)))
        self.account_entry = ttk.Entry(login_box, textvariable=self.account)
        self.account_entry.grid(row=0, column=1, sticky='ew')
        ttk.Label(login_box, text='비밀번호').grid(row=0, column=2, padx=px(8))
        self.password_entry = ttk.Entry(login_box, textvariable=self.password, show='●')
        self.password_entry.grid(row=0, column=3, sticky='ew')
        auto_box = self.auto_box = ttk.Frame(frame)
        auto_box.pack(fill='x', pady=px((0, 6)))
        self.login_button = ttk.Button(auto_box, text='로그인', command=self.sign_in)
        self.login_button.pack(side='left')
        self.logout_button = ttk.Button(auto_box, text='로그아웃', command=self.sign_out)
        self.logout_button.pack(side='left', padx=px(8))
        self.auto_login_button = ttk.Checkbutton(auto_box, text='다음 실행 시 자동로그인',
                                                variable=self.auto_login, command=self._auto_changed)
        self.auto_login_button.pack(side='left')
        ttk.Label(frame, textvariable=self.login_status, wraplength=px(750)).pack(anchor='w', pady=px((0, 6)))
        credential_buttons = self.credential_buttons = ttk.Frame(frame)
        credential_buttons.pack(fill='x', pady=px((0, 6)))
        self.save_password_button = ttk.Button(credential_buttons, text='비밀번호 저장', command=self.save_password)
        self.save_password_button.pack(side='left')
        self.forget_password_button = ttk.Button(credential_buttons, text='저장 비밀번호 지우기',
                                                command=self.forget_saved_password)
        self.forget_password_button.pack(side='left', padx=px(8))
        self.credential_note = ttk.Label(frame, text='자동로그인을 선택하고 로그인 또는 시작하면, 로그인 확인 후 계정과 비밀번호를 저장함. 비밀번호는 Windows 자격 증명 관리자에 보관함.',
                  wraplength=px(710))
        self.credential_note.pack(anchor='w')
        self.credential_status_label = ttk.Label(frame, textvariable=self.credential_status, wraplength=px(710))
        self.credential_status_label.pack(anchor='w', pady=px((4, 0)))
        if self._borrowed:
            for widget in (self.login_box, self.auto_box, self.credential_buttons,
                           self.credential_note, self.credential_status_label):
                widget.pack_forget()
            self.set_borrowed_session(self.login_result)
            self.status.set('대기 · 글 주소를 입력한 뒤 1회 시험 가능')
        duration_box = ttk.Frame(frame)
        duration_box.pack(fill='x', pady=px(10))
        ttk.Label(duration_box, text='반복 시간').pack(side='left')
        self.duration_entry = ttk.Spinbox(duration_box, from_=1, to=300, width=6, textvariable=self.minutes)
        self.duration_entry.pack(side='left', padx=px(8))
        self.duration_hint = ttk.Label(duration_box, text='분 · 최대 300분(5시간)')
        self.duration_hint.pack(side='left')
        ttk.Label(duration_box, text='  초당 최대').pack(side='left', padx=px((16, 0)))
        self.rate_entry = ttk.Spinbox(duration_box, from_=.1, to=2, increment=.1,
                                      width=6, textvariable=self.rate)
        self.rate_entry.pack(side='left', padx=px(8))
        ttk.Label(duration_box, text='회 (0.1~2)').pack(side='left')
        period_box = ttk.Frame(frame)
        period_box.pack(fill='x', pady=px((0, 6)))
        ttk.Label(period_box, text='끌올 주기').pack(side='left')
        self.period_entry = ttk.Combobox(period_box, textvariable=self.period, state='readonly',
                                         values=('초당 횟수', *PERIODS), width=14)
        self.period_entry.pack(side='left', padx=px(8))
        self.period_entry.bind('<<ComboboxSelected>>', self._period_changed)
        ttk.Label(frame, textvariable=self.target_label, wraplength=px(710)).pack(anchor='w', pady=px(4))
        ttk.Label(frame, textvariable=self.status, wraplength=px(710)).pack(anchor='w', pady=px(4))
        ttk.Label(frame, textvariable=self.counts, wraplength=px(710)).pack(anchor='w', pady=px(4))
        self.help_label = ttk.Label(frame, wraplength=px(710), justify='left', foreground='#616161',
                                    text=SINGLE_HELP + RUN_HELP)
        self.help_label.pack(anchor='w', pady=px(8))
        self.trial_button = ttk.Button(buttons, text='1회 시험', command=lambda: self.start(trial=True))
        self.trial_button.pack(side='left')
        self.start_button = ttk.Button(buttons, text='반복 시작', command=self.start)
        self.start_button.pack(side='left', padx=px(8))
        self.stop_button = ttk.Button(buttons, text='중지', state='disabled', command=self.stop)
        self.stop_button.pack(side='left')
        # Not packed: the mode radios at the top switch single/multi. Kept for its enable state.
        self.experiment_button = ttk.Button(buttons, text='여러 글', command=self.toggle_experiment)
        if not embedded:
            ttk.Button(buttons, text='닫기', command=self.close).pack(side='right')
        self.inputs = (self.url_entry, self.second_url_entry, self.account_entry,
                       self.password_entry, self.duration_entry, self.rate_entry, self.additional_urls)
        self.poll_id = self.window.after(80, self._poll)
        self._load_profile()
        self.account.trace_add('write', self._login_input_changed)
        self.password.trace_add('write', self._login_input_changed)
        if not embedded:
            monitor_window(self.window)

    def set_borrowed_session(self, login_result):
        self.login_result = login_result
        self._borrowed = True
        self.profile_store = None
        if login_result is not None:
            account = login_result.verified_user_id
            if self.account is not None:
                self.account.set(account)
            if self.login_status is not None:
                self.login_status.set(f'메인 창 로그인 계정 사용: {account} · 로그아웃·계정 변경은 메인 창에서')
        else:
            if self.account is not None:
                self.account.set('')
            if self.login_status is not None:
                self.login_status.set('메인 창에서 먼저 로그인 필요')

    def _enabled(self, value):
        state = 'normal' if value else 'disabled'
        for widget in (*self.inputs, self.trial_button, self.start_button,
                       self.save_password_button, self.forget_password_button, self.experiment_button,
                       self.single_mode_radio, self.multi_mode_radio,
                       self.login_button, self.logout_button, self.auto_login_button):
            widget.configure(state=state)
        self.period_entry.configure(state='readonly' if value else 'disabled')
        if value:
            self._period_changed()
        self.stop_button.configure(state='disabled' if value else 'normal')

    def _period_changed(self, *_):
        self.rate_entry.configure(state='normal' if self.period.get() == '초당 횟수' else 'disabled')

    def set_experimental(self, value):
        if bool(value) != self.experimental:
            self.toggle_experiment()
        elif self.mode_var is not None:
            self.mode_var.set(self.experimental)

    def toggle_experiment(self):
        if not self._credential_action_allowed():
            if self.mode_var is not None:
                self.mode_var.set(self.experimental)
            return
        self.experimental = not self.experimental
        if self.mode_var is not None:
            self.mode_var.set(self.experimental)
        # Retire completed display state so polling cannot restore old counters.
        self.task = None
        self._finished_reported = False
        if self.experimental:
            self.experiment_box.pack(after=self.url_entry, fill='x')
            if not self.embedded:
                self.window.title('정류기 — 여러 글 순환 끌올')
                size_window(self.window, 850, 860, minimum=(800, 840))
        else:
            self.experiment_box.pack_forget()
            if not self.embedded:
                self.window.title('정류기 — 지정 글 끌올')
                size_window(self.window, 850, 760, minimum=(800, 730))
        self.url_label.configure(text='1번 글 주소' if self.experimental else '마이너 갤러리 글 주소')
        self.trial_button.configure(text='전체 1회씩' if self.experimental else '1회 시험')
        self.rate.set('1' if self.experimental else '2')
        self.help_label.configure(text=(MULTI_HELP if self.experimental else SINGLE_HELP) + RUN_HELP)
        self.status.set('대기 · 2~20개 주소 입력 후 전체 1회씩 또는 반복 시작' if self.experimental
                        else '대기 · 글 주소를 입력한 뒤 1회 시험 가능' if self._borrowed
                        else '대기 · 글 주소와 관리 계정을 입력한 뒤 1회 시험 가능')
        self.target_label.set('대상: 아직 지정하지 않음')
        self.counts.set('요청 0회 · 성공 응답 0회 · 결과 미확정 0회')
        self.target_rows.delete(*self.target_rows.get_children())

    def _account_action_allowed(self):
        return not self._borrowed and self._credential_action_allowed()

    def _credential_action_allowed(self):
        return not (self.closed or self.close_requested or self.login_task is not None
                    or (self.task is not None and self.task.alive))

    def _cancel_auto_login(self):
        if self.auto_login_id is not None:
            self.window.after_cancel(self.auto_login_id)
            self.auto_login_id = None

    def _load_profile(self):
        if self.profile_store is None:
            return
        try:
            profile = self.profile_store.load()
            if not self.account.get():
                self.account.set(profile.last_account)
            self.auto_login.set(profile.auto_login and self.account.get() == profile.last_account)
            if self.auto_login.get():
                self.login_status.set('저장 계정 자동로그인 준비 중 · 끌올은 시작 버튼으로 실행')
                self.auto_login_id = self.window.after(350, self._startup_login)
        except Exception:
            record_error('bump_ui._load_profile')
            self.login_status.set('저장 계정 설정을 읽지 못함 · 직접 로그인 가능')

    def _startup_login(self):
        self.auto_login_id = None
        if not self.closed and not self.close_requested and self.auto_login.get():
            self.sign_in(automatic=True)

    def _drop_session(self):
        if self._borrowed:
            return
        result, self.login_result = self.login_result, None
        close_login(result)

    def _login_input_changed(self, *_):
        if self.closed:
            return
        self._cancel_auto_login()
        if self._credential_action_allowed() and self.login_result is not None and not self._borrowed:
            if (self.account.get().strip().lower() != self.login_result.verified_user_id
                    or self.password.get()):
                self._drop_session()
                self.login_status.set('계정 입력 변경됨 · 다시 로그인 필요')

    def _disable_auto(self):
        self.auto_login.set(False)
        self._cancel_auto_login()
        if not disable_auto_login(self.profile_store):
            self.credential_status.set('자동로그인 해제 저장 실패 · 다음 실행 시 자동로그인할 수 있음')

    def _auto_changed(self):
        if not self._account_action_allowed():
            return
        self._cancel_auto_login()
        if not self.auto_login.get():
            self._disable_auto()
        elif self.login_result is not None and self.profile_store is not None:
            try:
                self.profile_store.remember(self.login_result.verified_user_id, None,
                                            auto_login=True, write_password=False)
                self.credential_status.set('다음 실행 시 자동로그인 설정됨')
            except Exception:
                record_error('bump_ui._auto_changed')
                self.auto_login.set(False)
                self.credential_status.set('자동로그인 설정 저장 실패 · 이번 로그인은 사용 가능')
        else:
            self.credential_status.set('로그인 또는 시작을 누르면 로그인 확인 후 자동로그인 설정을 저장함')

    def sign_in(self, automatic=False, *, continue_bump=False):
        if not self._account_action_allowed():
            return
        self._cancel_auto_login()
        if not continue_bump:
            self._pending_start = None
        self._drop_session()
        self.task = None
        password = self.password.get()
        self.password.set('')
        try:
            options = {}
            if self.profile_store is not None:
                options['on_verified'] = remember_after_login(
                    self.profile_store, auto_login=self.auto_login.get(), write_password=bool(password))
            self.login_task = self.login_task_factory(self.account.get().strip(), password,
                                                      use_saved=not bool(password), **options)
            self._automatic_attempt = automatic
            self._enabled(False)
            self.login_status.set('자동로그인 중' if automatic else '로그인 중')
            self.login_task.start()
        except Exception:
            record_error('bump_ui.sign_in')
            self.login_task = self._pending_start = None
            self._enabled(True)
            self.login_status.set('로그인 시작 실패 · ID 확인 필요 · 자동 재시도 없음')
            if automatic:
                self._disable_auto()
        finally:
            password = None

    def sign_out(self):
        if not self._account_action_allowed():
            return
        self._disable_auto()
        self._drop_session()
        self.password.set('')
        self.login_status.set('로그아웃됨 · 자동로그인 해제됨')

    def _finish_login(self):
        task, self.login_task = self.login_task, None
        result = task.take_result()
        pending, self._pending_start = self._pending_start, None
        self._enabled(True)
        if result is not None:
            self.login_result = result
            if (self.close_requested or task.cancel.is_set()
                    or result.verified_user_id != self.account.get().strip().lower()):
                self._drop_session()
                pending = None
                self.login_status.set('로그인 취소됨')
            else:
                self.login_status.set(f'{result.verified_user_id} 로그인됨 · 시작 버튼으로 끌올 실행')
                if task.profile_warning:
                    self.credential_status.set(task.profile_warning)
                elif task.profile_saved:
                    self.credential_status.set('로그인 계정 저장됨 · 다음 실행 시 자동로그인함'
                                               if self.auto_login.get() else '로그인 계정 저장됨')
        else:
            pending = None
            self.login_status.set('로그인 종료 · ' + (task.failure_message or REASONS.get(
                task.state.reason, '로그인 실패 · 자동 재시도 없음')))
            if self._automatic_attempt:
                self._disable_auto()
        self._automatic_attempt = False
        if self.close_requested:
            self._finish_close()
        elif pending is not None and self.login_result is not None:
            self._dispatch(*pending)

    def save_password(self):
        if not self._account_action_allowed():
            return
        password = None
        try:
            _, account = _identity(self.account.get().strip())
            password = self.password.get()
            if not password:
                self.credential_status.set('저장할 비밀번호를 입력한 뒤 저장 버튼을 누를 것')
                return
            self.password_saver(account, password)
            self.password.set('')
            self.credential_status.set(f'{account} 저장 완료 · 다음부터 비밀번호를 비워도 됨')
        except CredentialError as error:
            message = str(error)
            if message == '기존 정류기 비밀번호가 있음. 덮어쓰지 않았음':
                message += ' · 교체하려면 저장 비밀번호 지우기 후 다시 저장'
            self.credential_status.set(message)
        except Exception:
            record_error('bump_ui.save_password')
            self.credential_status.set('비밀번호 저장 실패 · 입력한 비밀번호로 실행 가능')
        finally:
            password = None

    def forget_saved_password(self):
        if not self._account_action_allowed():
            return
        try:
            _, account = _identity(self.account.get().strip())
            confirmed = self.confirm_forget(
                '저장 비밀번호 지우기',
                f'{account}의 저장 비밀번호를 지울 것인지 확인 필요.\n\n'
                '메인 정류기도 이 계정의 저장 비밀번호를 함께 사용함.\n'
                '삭제 후에는 비밀번호를 직접 입력하거나 다시 저장해야 함.\n'
                '비밀번호를 모르면 취소할 것. 디시 계정의 비밀번호 자체는 변경되지 않음.',
                parent=self.toplevel, default='no')
            if not confirmed:
                self.credential_status.set('저장 비밀번호 지우기 취소됨')
                return
            if not self._credential_action_allowed():
                return
            removed = self.password_remover(account)
            self._drop_session()
            self.login_status.set('저장 비밀번호 삭제 처리됨 · 다시 로그인 필요')
            self.credential_status.set(
                f'{account} 저장 비밀번호 지움 · 직접 입력 또는 새 비밀번호 저장 가능'
                if removed else f'{account}에 저장된 비밀번호 없음')
            self._disable_auto()
        except CredentialError as error:
            self.credential_status.set(str(error))
        except Exception:
            record_error('bump_ui.forget_saved_password')
            self.credential_status.set('저장 비밀번호 지우기 실패 · 자동 재시도하지 않음')

    def start(self, trial=False):
        if not self._credential_action_allowed():
            return
        if self.before_start is not None and not self.before_start():
            return
        try:
            target = BumpTarget.from_url(self.url.get())
            second = None
            additional = ()
            if self.experimental:
                second = BumpTarget.from_url(self.second_url.get())
                lines = [line.strip() for line in self.additional_urls.get('1.0', 'end-1c').splitlines() if line.strip()]
                additional = tuple(BumpTarget.from_url(line) for line in lines)
                validate_bump_targets((target, second, *additional))
            _, account = _identity(self.account.get().strip())
            if self.period.get() == '초당 횟수':
                rate = float(self.rate.get())
                if not isfinite(rate) or not .1 <= rate <= 2:
                    raise ValueError('초당 횟수는 0.1~2회여야 함')
                interval = 1 / rate
            elif self.period.get() in PERIODS:
                interval = PERIODS[self.period.get()]
            else:
                raise ValueError('끌올 주기 선택 필요')
            if trial:
                count = 2 + len(additional) if self.experimental else 1
                trial_duration = max(60, ceil((count - 1) * (interval + .2)) + 60)
                if trial_duration > 18000:
                    self.status.set('전체 1회씩 시험에 5시간 넘게 필요함 · 글 수나 간격을 줄이거나 반복 시작을 사용할 것')
                    return
                limits = BumpLimits(interval=interval, jitter=.2,
                                    duration=trial_duration, max_requests=count)
            else:
                minutes = int(self.minutes.get())
                if not 1 <= minutes <= 300:
                    raise ValueError('반복 시간은 1~300분이어야 함')
                limits = BumpLimits(interval=interval, jitter=.2,
                                    duration=minutes * 60,
                                    max_requests=ceil(minutes * 60 / interval))
        except (ValueError, TypeError, CredentialError):
            self.status.set('입력 확인 필요 · 같은 갤러리의 서로 다른 글 주소 2~20개, 로그인 ID, 시간 1~300분, 초당 0.1~2회 확인'
                            if self.experimental else '입력 확인 필요 · 글 주소, 로그인 ID, 시간 1~300분, 초당 0.1~2회 확인')
            return
        self._cancel_auto_login()
        if self.login_result is None and self.profile_store is not None:
            self._pending_start = (target, second, account, limits, additional)
            self.sign_in(continue_bump=True)
        else:
            self._dispatch(target, second, account, limits, additional)

    def _dispatch(self, target, second, account, limits, additional=()):
        password = None
        try:
            extra = {}
            if second is not None:
                extra['second_target'] = second
            if additional:
                extra['additional_targets'] = additional
            if self.login_result is not None:
                extra['login_result'] = self.login_result
            else:
                password = self.password.get() or None
            task = self.task_factory(target, account, limits, password=password, **extra)
            self.password.set('')
            self.task = task
            self._finished_reported = False
            self.target_label.set(f'대상: {target.gallery} / {2 + len(additional)}개 글 · 입력 순서대로 반복'
                                  if self.experimental else f'대상: {target.gallery} / 글 {target.post_id}')
            self.target_rows.delete(*self.target_rows.get_children())
            if second is not None:
                for i, item in enumerate((target, second, *additional)):
                    self.target_rows.insert('', 'end', iid=str(i), values=(f'{i + 1} / {item.post_id}', '확인 중', 0))
            self.counts.set('요청 0회 · 성공 응답 0회 · 결과 미확정 0회')
            self.status.set('로그인·대상·끌올 권한 확인 중')
            self._enabled(False)
            task.start()
            if self.on_started is not None:
                self.on_started()
        except Exception:
            record_error('bump_ui._dispatch')
            if self.task is not None:
                self.task.stop()
            self.status.set('시작 실패 · 자동 재시도하지 않음')
            self._enabled(True)
        finally:
            password = None

    def stop(self):
        self._cancel_auto_login()
        self._pending_start = None
        active = self.login_task or self.task
        if active is not None:
            active.stop()
            self.status.set('중지 대기 · 진행 중인 통신 종료 후 추가 요청 없이 끝남')
            self.stop_button.configure(state='disabled')

    def _poll(self):
        self.poll_id = None
        if self.closed:
            return
        task = self.login_task or self.task
        if task is not None:
            try:
                request = task.captchas.get_nowait()
            except Empty:
                pass
            else:
                try:
                    if request.image and not task.cancel.is_set():
                        request.answer = prompt_captcha(request.image, parent=self.toplevel,
                                                        cancelled=task.cancel.is_set)
                except Exception:
                    record_error('bump_ui._poll')
                    request.answer = None
                finally:
                    request.image = b''
                    request.ready.set()
            if self.login_task is not None:
                if task.state.stage == 'finished' and not task.alive:
                    self._finish_login()
                elif not task.cancel.is_set():
                    self.login_status.set('보안문자 입력 대기' if task.state.stage == 'captcha' else '로그인 중')
                if not self.closed:
                    self.poll_id = self.window.after(80, self._poll)
                return
            state = task.state
            if state.stats is not None:
                s = state.stats
                self.counts.set(f'요청 {s.requests}회 · 성공 응답 {s.acknowledged}회 · 결과 미확정 {s.unknown}회\n'
                                f'경과 {s.elapsed:.1f}초 · 평균 {s.rate:.2f}회/초 · 최근 요청 {s.last_ms:.0f}ms')
            if task.title:
                self.target_label.set(f'대상: {task.target.gallery} / 글 {task.target.post_id} · {task.title}')
            pair = getattr(task, 'pair_progress', None)
            if pair is not None:
                targets = getattr(task, 'targets', (task.target, task.second_target))
                for i, item in enumerate(targets):
                    if self.target_rows.exists(str(i)):
                        self.target_rows.item(str(i), values=(f'{i + 1} / {item.post_id}', pair.titles[i], pair.acknowledged[i]))
                if len(targets) == 2:
                    self.target_label.set(f'A: 글 {task.target.post_id} · {pair.titles[0]}\n'
                                          f'B: 글 {task.second_target.post_id} · {pair.titles[1]}')
                    last = '없음' if pair.last_index is None else 'AB'[pair.last_index]
                    self.counts.set(self.counts.get() + f'\nA 성공 {pair.acknowledged[0]}회 · '
                                    f'B 성공 {pair.acknowledged[1]}회 · 최근 요청 {last}')
                else:
                    last = '없음' if pair.last_index is None else f'{pair.last_index + 1}번'
                    self.counts.set(self.counts.get() + f'\n등록 {len(targets)}개 · 최근 요청 {last} · 글별 성공 횟수는 위 목록 참고')
            if state.stage == 'finished' and not task.alive:
                if not self._finished_reported:
                    self.status.set('종료 · ' + REASONS.get(state.reason, '결과 확인 필요'))
                    self._enabled(True)
                    self._finished_reported = True
                    if state.reason not in {'request_limit', 'duration_limit', 'user_stopped'}:
                        self._drop_session()
                        self.login_status.set('실행 오류 · 메인 창에서 로그인·관리 권한을 다시 확인할 것'
                                              if self._borrowed else '실행 오류로 세션 종료됨 · 자동 재시도 없음')
                        if state.reason == 'auth_required':
                            self._disable_auto()
                    if self.on_stopped is not None:
                        self.on_stopped()
                if self.close_requested:
                    self._finish_close()
                    return
            elif not task.cancel.is_set():
                self.status.set({'starting': '준비 중', 'login': '로그인 중', 'captcha': '보안문자 입력 대기',
                                 'checking': '대상·끌올 권한 확인 중',
                                 'running': '끌올 반복 중'}.get(state.stage, '준비 중'))
        self.poll_id = self.window.after(80, self._poll)

    def close(self):
        if self.closed:
            return
        self.password.set('')
        self._cancel_auto_login()
        self._pending_start = None
        if self.login_task is not None or (self.task is not None and self.task.alive):
            self.close_requested = True
            self.stop()
            return
        self._finish_close()

    def _finish_close(self):
        self._cancel_auto_login()
        self._drop_session()
        self.closed = True
        if self.poll_id is not None:
            self.window.after_cancel(self.poll_id)
        if self.on_stopped is not None:
            self.on_stopped()
        self.window.destroy()
        self.mode_var = None
        self.url = self.second_url = self.account = self.password = self.minutes = self.rate = self.period = None
        self.auto_login = self.login_status = None
        self.status = self.counts = self.target_label = self.credential_status = None
        callback, self.on_closed = self.on_closed, None
        if callback is not None:
            callback()


def run_bump_ui():
    root = tk.Tk()
    root.withdraw()
    # Root owns the Python reference until all worker and Tk work has ended.
    root.bump_window = BumpWindow(root, root.destroy,
                                  profile_store=AccountProfiles(DATA_ROOT / 'bump-accounts.sqlite'))
    root.mainloop()
    return 0
