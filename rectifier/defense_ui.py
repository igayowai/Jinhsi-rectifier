"""Explicit start/stop defense view (tab-embedded or standalone window)."""
from queue import Empty
from time import monotonic
import tkinter as tk
from tkinter import ttk

from . import APP_NAME
from .captcha_ui import prompt_captcha
from .defense_task import DefenseTask
from .defense_monitor import DefenseMonitor, format_monitor
from .dpi import apply_theme, pixels, size_window, monitor_window
from .diagnostics import record_error


REASONS = {
    'user_stopped': '사용자 중지',
    'auth_required': '로그인이 풀림 · 로그아웃 후 다시 로그인하세요',
    'forbidden': '이 갤러리의 관리 삭제 권한이 없음 · 갤러리와 계정을 확인하세요',
    'rate_limited': '서버가 요청을 잠시 제한함 · 몇 분 뒤 다시 시작하세요',
    'quota_exhausted': '오늘 삭제 한도에 도달함 · 내일 다시 시도하세요',
    'stale_listing': '목록 확인이 늦어져 안전하게 멈춤 · 다시 시작하면 됨',
    'invalid_listing': '목록을 읽지 못함 · 갤러리 주소가 맞는지 확인하세요',
    'read_failed': '인터넷 연결이 불안정함 · 연결 확인 후 다시 시작하세요',
    'empty_response': '서버가 빈 응답을 보냄 · 권한을 판단할 수 없어 멈춤, 잠시 뒤 다시 시도하세요',
    'scope_mismatch': '실행 범위가 바뀜 · 창을 닫고 다시 여세요',
    'baseline_unavailable': '최신 글을 확인하지 못해 삭제 없이 멈춤 · 다시 시작하세요',
    'response_unknown': '삭제가 됐는지 확인하지 못함 · 갤러리 관리 내역에서 확인하세요',
    'rejection_reported': '서버가 삭제 요청을 거부함 · 관리 권한을 확인하세요',
    'response_mismatch': '삭제 응답이 요청과 맞지 않아 멈춤 · 관리 내역에서 확인하세요',
    'tracking_capacity': '한 번에 기억할 수 있는 글 수를 넘음 · 다시 시작하면 됨',
    'queue_capacity': '처리 대기 글이 너무 많아 멈춤 · 다시 시작하면 됨',
    'credential_missing': '저장된 비밀번호가 없음 · 비밀번호를 입력해 로그인하세요',
    'worker_failed': '예상하지 못한 오류로 멈춤 · 다시 시작해 보고, 반복되면 진단 로그를 확인하세요',
}


def _clock(seconds):
    seconds = int(max(0, seconds))
    hours, rest = divmod(seconds, 3600)
    return f'{hours}:{rest // 60:02d}:{rest % 60:02d}' if hours else f'{rest // 60}:{rest % 60:02d}'


def activity_line(elapsed, last_accept_age):
    last = '아직 없음' if last_accept_age is None else f'{_clock(last_accept_age)} 전'
    return f'경과 {_clock(elapsed)} · 마지막 삭제 수락 {last}'


class DefenseWindow:
    def __init__(self, parent, gallery, pages, on_closed=lambda: None, *, task_factory=DefenseTask,
                 account=None, embedded=False, before_start=None, on_started=None, on_stopped=None):
        # The manual browsing range must never determine live defense scope.
        self.gallery, self.pages = gallery, (1,)
        self.account = account
        self.embedded = embedded
        self.before_start = before_start
        self.on_started = on_started
        self.on_stopped = on_stopped
        self.on_closed, self.task_factory = on_closed, task_factory
        self.task = None
        self.monitor = None
        self._start_failed = False
        self._finish_notified = False
        self.closed = self.close_requested = False
        apply_theme(parent)
        if embedded:
            self.window = ttk.Frame(parent)
            self.window.pack(fill='both', expand=True)
            self.window.withdraw = lambda: None
            self.window.deiconify = lambda: None
            self.window.title = lambda *_: ''
            self.window.protocol = lambda *_: None
            self.window.transient = lambda *_: None
            self.window.grab_set = lambda *_: None
            self.toplevel = parent.winfo_toplevel()
        else:
            self.window = tk.Toplevel(parent)
            self.toplevel = self.window
            self.window.title(f'{APP_NAME} — 방어모드')
            size_window(self.window, 780, 470, minimum=(680, 440))
            self.window.transient(parent)
            self.window.grab_set()
            self.window.protocol('WM_DELETE_WINDOW', self.close)
        self.px = lambda value: pixels(self.window, value)
        self.context_text = tk.StringVar(self.window, self._format_context())
        self.status = tk.StringVar(self.window, '대기 · 방어 시작을 누르면 최신 1페이지 기준을 잡고 감시를 시작함')
        self.counts = tk.StringVar(self.window, '목록 조회 0회 · 삭제 요청 0회 · 요청 수락 누적 0건')
        self.log_status = tk.StringVar(self.window, '')
        self.metric_elapsed = tk.StringVar(self.window, '경과 0:00\n마지막 수락 아직 없음')
        self.metric_speed = tk.StringVar(self.window, '집계 대기\n최근 처리 0ms')
        self.metric_totals = tk.StringVar(self.window, '수락 누적 0건\n요청 0회 · 조회 0회')
        self.metric_candidates = tk.StringVar(self.window, '후보 0개 · 미배정 0개\n응답 불명 0회')
        self.include_new_members = tk.BooleanVar(self.window, False)
        self._running_include_new_members = False
        self._accepted_seen, self._accepted_at = 0, None
        frame = ttk.Frame(self.window, padding=self.px(22))
        frame.pack(fill='both', expand=True)
        # Reserve actions before packing the body, including on short displays.
        buttons = ttk.Frame(frame)
        buttons.pack(fill='x', side='bottom', pady=self.px(8))
        self.start_button = ttk.Button(buttons, text='방어 시작', style='Accent.TButton', command=self.start)
        self.start_button.pack(side='left')
        self.stop_button = ttk.Button(buttons, text='방어 중지', state='disabled', command=self.stop)
        self.stop_button.pack(side='left', padx=self.px(10))
        if not embedded:
            ttk.Button(buttons, text='닫기', command=self.close).pack(side='right')
        viewport = ttk.Frame(frame)
        viewport.pack(fill='both', expand=True)
        self.body_canvas = tk.Canvas(viewport, height=self.px(120), highlightthickness=0, borderwidth=0,
            background=ttk.Style(self.window).lookup('TFrame', 'background') or '#FAFAFA')
        scrollbar = ttk.Scrollbar(viewport, orient='vertical', command=self.body_canvas.yview)
        scrollbar.pack(side='right', fill='y')
        self.body_canvas.configure(yscrollcommand=scrollbar.set)
        self.body_canvas.pack(side='left', fill='both', expand=True)
        inner = ttk.Frame(self.body_canvas)
        body = self.body_canvas.create_window((0, 0), window=inner, anchor='nw')
        def resize_body(event):
            self.body_canvas.itemconfigure(body, width=event.width)
            for widget in inner.winfo_children():
                if isinstance(widget, ttk.Label) and widget.cget('wraplength'):
                    widget.configure(wraplength=max(1, event.width))
        self.body_canvas.bind('<Configure>', resize_body)
        inner.bind('<Configure>', lambda _event: self.body_canvas.configure(
            scrollregion=self.body_canvas.bbox('all')))
        if not embedded:
            self.window.bind('<MouseWheel>', self._scroll_body, add='+')
        settings_card = ttk.LabelFrame(inner, text='방어 설정 및 제어', padding=self.px(12))
        settings_card.pack(fill='x', pady=self.px((0, 10)))
        ttk.Label(settings_card, textvariable=self.context_text, font=('맑은 고딕', 10)).pack(
            anchor='w', pady=self.px((0, 6)))
        scope = ttk.Frame(settings_card)
        scope.pack(fill='x', pady=self.px(4))
        ttk.Label(scope, text='감시 페이지 수 (최신 1p부터)').pack(side='left')
        self.page_count = tk.StringVar(self.window, '1')
        self.page_count_box = ttk.Combobox(scope, textvariable=self.page_count,
            values=('1', '2', '3', '4', '5'), state='readonly', width=4)
        self.page_count_box.pack(side='left', padx=self.px(8))
        ttk.Label(scope, text='(기본 1p · 도배가 심할 때만 2p)').pack(side='left', padx=self.px((4, 16)))
        self.new_members_box = ttk.Checkbutton(scope, text='회원 깡계도 제거 대상에 포함',
                                               variable=self.include_new_members)
        self.new_members_box.pack(side='left')
        status_card = ttk.LabelFrame(inner, text='현재 상태', padding=self.px(12))
        status_card.pack(fill='x', pady=self.px((0, 10)))
        ttk.Label(status_card, textvariable=self.status, font=('맑은 고딕', 11, 'bold'),
                  wraplength=self.px(710)).pack(anchor='w')
        ttk.Label(status_card, textvariable=self.log_status, wraplength=self.px(710)).pack(
            anchor='w', pady=self.px((4, 0)))
        cards = ttk.Frame(inner)
        cards.pack(fill='x', pady=self.px((0, 8)))
        for col, (heading, var) in enumerate((
            ('경과 · 마지막 수락', self.metric_elapsed),
            ('처리 속도 (최근 10초)', self.metric_speed),
            ('누적 처리', self.metric_totals),
            ('현재 후보 · 응답', self.metric_candidates),
        )):
            cards.columnconfigure(col, weight=1)
            card = ttk.LabelFrame(cards, text=heading, padding=self.px(10))
            card.grid(row=0, column=col, sticky='nsew', padx=self.px((0 if col == 0 else 6, 0)))
            ttk.Label(card, textvariable=var, justify='left').pack(anchor='w')
        self.counts_label = ttk.Label(inner, textvariable=self.counts, wraplength=self.px(710), justify='left')
        self.counts_label.pack(anchor='w', pady=self.px(4))
        self.poll_id = self.window.after(80, self._poll)
        if not embedded:
            monitor_window(self.window)

    def _format_context(self):
        return f'갤러리: {self.gallery}  ·  계정: {self.account or "로그인 필요"}'

    def set_context(self, gallery=None, account=None):
        if self.closed:
            return
        if gallery is not None:
            self.gallery = gallery
        self.account = account
        if self.context_text is not None:
            self.context_text.set(self._format_context())

    def _enabled(self, value):
        if self.closed:
            return
        if value and not self._start_failed:
            self.page_count_box.configure(state='readonly')
            self.new_members_box.configure(state='normal')
            self.start_button.configure(state='normal')
            self.stop_button.configure(state='disabled')
        elif not value:
            self.page_count_box.configure(state='disabled')
            self.new_members_box.configure(state='disabled')
            self.start_button.configure(state='disabled')

    def _scroll_body(self, event):
        if self.body_canvas.yview() != (0.0, 1.0) and event.delta:
            self.body_canvas.yview_scroll(-1 if event.delta > 0 else 1, 'units')
        return 'break'

    def _update_cards(self, summary, age):
        if self.metric_elapsed is None:
            return
        last = '아직 없음' if age is None else f'{_clock(age)} 전'
        self.metric_elapsed.set(f"경과 {_clock(summary['elapsed_seconds'])}\n마지막 수락 {last}")
        rate = summary['accepted_per_second']
        speed = '속도 집계 중' if rate is None else f"{rate:.1f} 건/초 ({summary['window_seconds']:.0f}초)"
        self.metric_speed.set(f"{speed}\n최근 처리 {summary['last_cycle_ms']:.0f}ms")
        self.metric_totals.set(
            f"수락 누적 {summary['accepted_unverified']}건\n"
            f"요청 {summary['delete_requests']}회 · 조회 {summary['scans']}회")
        self.metric_candidates.set(
            f"후보 {summary['last_candidates']}개 · 미배정 {summary['last_deferred']}개\n"
            f"응답 불명 {summary['unknown_responses']}회")

    def start(self):
        if self.closed:
            return
        if self.task is not None and self.task.alive:
            return
        if not self.embedded and self.monitor is not None:
            return
        if self.embedded and (self._start_failed or (self.monitor is not None and not self.monitor.finished)):
            return
        if self.before_start is not None and not self.before_start():
            return
        if self.page_count.get() not in ('1', '2', '3', '4', '5'):
            self.status.set('감시 페이지 수는 1~5 중 선택해야 함 · 요청 없음')
            return
        self.pages = tuple(range(1, int(self.page_count.get()) + 1))
        self.page_count_box.configure(state='disabled')
        self._running_include_new_members = self.include_new_members.get()
        self.new_members_box.configure(state='disabled')
        self.start_button.configure(state='disabled')
        self.stop_button.configure(state='normal')
        self._accepted_seen, self._accepted_at = 0, None
        self._finish_notified = False
        if self.on_started is not None:
            self.on_started()
        self.monitor = DefenseMonitor()
        self.monitor.sample(None, stage='starting')
        self.monitor.save_if_due(force=True)
        self.log_status.set(self.monitor.log_status)
        failure_reason = 'task_creation_failed'
        try:
            self.task = self.task_factory(self.gallery, self.pages,
                include_new_members=self._running_include_new_members)
            failure_reason = 'task_start_failed'
            self.task.start()
        except Exception:
            record_error('defense_ui.start')
            self.monitor.sample(None, finished=True, reason=failure_reason, stage='finished')
            self.monitor.save_if_due()
            self.log_status.set(self.monitor.log_status)
            # A failed thread start leaves a task in 'starting'. Do not let
            # the poll overwrite the failure message with that stale state.
            self.task = None
            self._start_failed = True
            self.status.set('방어 작업 시작 실패 · 자동 재시도하지 않음')
            self.stop_button.configure(state='disabled')
            if self.embedded:
                self.page_count_box.configure(state='readonly')
                self.new_members_box.configure(state='normal')
                self.start_button.configure(state='normal')
            if self.on_stopped is not None:
                self.on_stopped()

    def stop(self):
        if self.task is not None and self.task.alive:
            self.task.stop()
            self.status.set('중지 대기 · 진행 중인 통신을 끝내는 중')
            self.stop_button.configure(state='disabled')

    def _poll(self):
        self.poll_id = None
        if self.closed:
            return
        task = self.task
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
                    record_error('defense_ui._poll')
                    request.answer = None
                finally:
                    request.image = b''
                    request.ready.set()
            state = task.state
            if self.monitor is not None:
                summary = self.monitor.sample(state.stats, state.updated_at,
                    finished=state.stage == 'finished' and not task.alive, reason=state.reason,
                    stage=state.stage)
                accepted = summary['accepted_unverified']
                if accepted > self._accepted_seen:
                    self._accepted_seen, self._accepted_at = accepted, monotonic()
                age = None if self._accepted_at is None else monotonic() - self._accepted_at
                self.counts.set(activity_line(summary['elapsed_seconds'], age) + '\n' + format_monitor(summary))
                self._update_cards(summary, age)
                self.monitor.save_if_due()
                self.log_status.set(self.monitor.log_status)
            if state.stage == 'finished' and not task.alive:
                self.status.set('방어 종료 · ' + REASONS.get(state.reason, '결과 확인 필요'))
                self.stop_button.configure(state='disabled')
                if self.embedded:
                    self.page_count_box.configure(state='readonly')
                    self.new_members_box.configure(state='normal')
                    self.start_button.configure(state='normal')
                if not self._finish_notified:
                    self._finish_notified = True
                    if self.on_stopped is not None:
                        self.on_stopped()
                if self.close_requested:
                    self._finish_close()
                    return
            elif not task.cancel.is_set():
                running = ('방어 실행 중 · 비회원 + 신규 딱지 회원' if self._running_include_new_members
                           else '방어 실행 중 · 비회원')
                stats = state.stats
                if state.stage == 'running' and stats is not None and not getattr(stats, 'last_candidates', 0):
                    running += ' · 새 글 없음 · 정상 감시 중'
                self.status.set({'starting': '방어 준비 중', 'login': '로그인 중',
                                 'captcha': '보안문자 입력 대기', 'baseline': '최신 글 기준 확인 중 · 삭제하지 않음',
                                 'running': running}.get(state.stage, '방어 준비 중'))
        self.poll_id = self.window.after(80, self._poll)

    def close(self):
        if self.closed:
            return
        if self.task is not None and self.task.alive:
            self.close_requested = True
            self.stop()
            return
        self._finish_close()

    def _finish_close(self):
        # Closing can beat the next 80ms poll after the worker has finished.
        if self.monitor is not None and self.task is not None:
            state = self.task.state
            if state.stage == 'finished':
                self.monitor.sample(state.stats, state.updated_at, finished=True, reason=state.reason,
                                    stage='finished')
                self.monitor.save_if_due()
        self.closed = True
        if self.poll_id is not None:
            try:
                self.window.after_cancel(self.poll_id)
            except tk.TclError:
                pass
            self.poll_id = None
        try:
            self.window.destroy()
        except tk.TclError:
            pass
        # Release Tk variables on their owning thread before a worker can run
        # cyclic GC and before the parent's close callback destroys Tcl.
        self.context_text = self.status = self.counts = self.log_status = None
        self.metric_elapsed = self.metric_speed = self.metric_totals = self.metric_candidates = None
        self.include_new_members = self.page_count = None
        if not self._finish_notified and self.on_stopped is not None:
            self._finish_notified = True
            self.on_stopped()
        callback, self.on_closed = self.on_closed, None
        if callback is not None:
            callback()
