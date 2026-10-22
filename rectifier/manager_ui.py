"""Main window: account, list browsing, manual delete, recommendation release, defense and bump.

The window lends its login session to exactly one worker at a time. Worker completion,
not a cancel click, releases it for logout or another operation.
"""
from queue import Empty, Queue
from threading import Event, Thread
import sys
import tkinter as tk
from tkinter import ttk, messagebox

from . import __version__, APP_NAME, APP_DESCRIPTION
from .account_task import LoginTask
from .account_profiles import Profiles, close_login, disable_auto_login, remember_after_login
from .batch_journal import BatchResponse
from .branding import LOGO_SIZES, logo_path, set_window_icon
from .bump import BumpTarget, MAX_BUMP_TARGETS
from .bump_ui import BumpWindow
from .captcha_ui import prompt_captcha
from .defense_runner import ListDefenseAdapter, DefenseReadError
from .defense_task import DefenseTask
from .defense_ui import DefenseWindow, REASONS
from .dpi import apply_theme, pixels, size_window, monitor_window
from .manual_action import ManualAction, ManualReceipt, format_manual_result
from .manual_delete import validate_review
from .models import validate_gallery
from .page_preview import preview_pages
from .performance import ManualPerformance
from .policy import MAX_MANUAL_TARGETS
from .public_board import read_public_board
from .recommend_action import RecommendReleaseAction, format_release_result
from .selection import ManualSelection, parse_pages
from .diagnostics import record_error


# No built-in galleries: IDs the user types are remembered in the recent list.
GALLERIES = {}
AUTHORS = {"delete_guest": "비회원", "protected_member": "회원 · 반고닉 포함",
           "unknown_author": "판별 불가", "conflicting_metadata": "정보 충돌"}
KINDS = {"ordinary": "일반글", "recommended": "개념글", "unknown": "종류 불명", "conflicting": "종류 충돌"}
KNOWN_AUTHORS = frozenset({'delete_guest', 'protected_member'})
DEFENSE_OFF, DEFENSE_ON = '#616161', '#0f7b0f'
TAB_LIST, TAB_DEFENSE, TAB_BUMP = '  목록 정리  ', '  자동 방어  ', '  끌올  '
RUNNING = ' (실행 중)'
RANGES = (('최신 1페이지', '1'), ('1~3페이지', '1,2,3'), ('1~5페이지', '1,2,3,4,5'))

# Every UI phase in one place. None means idle. A phase other than None owns the session.
PHASES = frozenset({None, 'login', 'permission', 'ready', 'executing',
                    'recommend_ready', 'recommend_executing', 'defense', 'bump'})
# Phases the footer "중지 / 취소" button can act on. Defense and bump have their own tab and stop button.
CANCELLABLE = frozenset({'login', 'permission', 'ready', 'executing',
                         'recommend_ready', 'recommend_executing'})


class ManagerUI:
    def __init__(self, root, *, reader=read_public_board, defense_task_factory=None,
                 bump_window_factory=BumpWindow, login_task_factory=LoginTask, action_factory=ManualAction,
                 release_factory=RecommendReleaseAction, profile_store=None, gallery_store=None):
        self.root, self.reader = root, reader
        self.px = lambda value: pixels(root, value)
        self.defense_task_factory = defense_task_factory
        self.bump_window_factory = bump_window_factory
        self._installed_bump_factory = None
        self.login_task_factory, self.action_factory = login_task_factory, action_factory
        self.release_factory = release_factory
        self.profile_store = profile_store
        self.gallery_store = gallery_store
        self.profiles = Profiles()
        self.recent_galleries = ()
        self.defense_window = self.bump_window = None
        self.login_task = self.login_result = None
        self.close_requested = False
        self.auto_login_id = None
        self.automatic_attempt = False
        self.delete_messages = Queue()
        self.delete_cancel = Event()
        self.delete_phase = None
        self.prepared = None
        self.delete_review = None
        self.model = ManualSelection()
        self.messages = Queue()
        self.cancel = Event()
        self.busy = self.closed = False
        self.review_window = None
        self.gallery = tk.StringVar(root, next(iter(GALLERIES), ''))
        self.pages = tk.StringVar(root, "1,2,3,4,5")
        self.status = tk.StringVar(root, "갤러리와 페이지를 지정한 뒤 조회를 누르세요.")
        self.detail = tk.StringVar(root, "공개 목록 조회 · 로그인 불필요 · 공지/광고 제외")
        self.count = tk.StringVar(root, "조회 0개  |  선택 0개")
        self.ids = tk.StringVar(root, "선택한 글 번호: 없음")
        self.selection_hint = tk.StringVar(root, "")
        self.account_text = tk.StringVar(root)
        self.password_text = tk.StringVar(root)
        self.use_saved = tk.BooleanVar(root, False)
        self.remember_account = tk.BooleanVar(root, True)
        self.auto_login = tk.BooleanVar(root, True)
        self.account_status = tk.StringVar(root, '로그인 전 · 본인 관리 계정으로 로그인')
        self.permission_status = tk.StringVar(root, '관리 삭제 권한 미확인')
        self.defense_state = tk.StringVar(root, '● 방어 꺼짐')
        self._build()
        monitor_window(self.root)
        self._delete_state(None)
        self.gallery.trace_add("write", self._scope_changed)
        self.pages.trace_add("write", self._scope_changed)
        self.account_text.trace_add('write', self._account_changed)
        self.password_text.trace_add('write', self._password_changed)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.poll_id = self.root.after(60, self._poll)
        self._load_galleries()
        self._load_profiles(startup=True)

    # ------------------------------------------------------------------ layout
    def _build(self):
        suffix = '' if getattr(sys, 'frozen', False) else ' (개발용)'
        self.root.title(f"{APP_NAME} {__version__} — 수동·방어 관리{suffix}")
        set_window_icon(self.root)
        apply_theme(self.root)
        size_window(self.root, 1120, 850, minimum=(960, 740))
        style = ttk.Style(self.root)
        style.configure("Treeview", rowheight=self.px(30), font=("맑은 고딕", 10))
        style.configure("Treeview.Heading", font=("맑은 고딕", 10, "bold"))
        self._build_top_bar()
        main = ttk.Frame(self.root, padding=self.px((18, 8, 18, 14)))
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(0, weight=1)
        self.notebook = ttk.Notebook(main)
        self.notebook.grid(row=0, column=0, sticky="nsew")
        self.list_tab = ttk.Frame(self.notebook, padding=self.px(12))
        self.defense_tab = ttk.Frame(self.notebook, padding=self.px(6))
        self.bump_tab = ttk.Frame(self.notebook, padding=self.px(6))
        self.notebook.add(self.list_tab, text=TAB_LIST)
        self.notebook.add(self.defense_tab, text=TAB_DEFENSE)
        self.notebook.add(self.bump_tab, text=TAB_BUMP)
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self._build_list_tab(self.list_tab)
        status_bar = ttk.Frame(main, padding=self.px((4, 10, 4, 0)))
        status_bar.grid(row=1, column=0, sticky="ew")
        status_bar.columnconfigure(0, weight=1)
        status_texts = ttk.Frame(status_bar)
        status_texts.grid(row=0, column=0, sticky="ew")
        ttk.Label(status_texts, textvariable=self.status, font=("맑은 고딕", 10, "bold"),
                  wraplength=self.px(920)).pack(anchor="w")
        ttk.Label(status_texts, textvariable=self.detail, wraplength=self.px(920)).pack(
            anchor="w", pady=self.px((2, 0)))
        self.cancel_button = ttk.Button(status_bar, text="중지 / 취소", command=self.cancel_delete, state="disabled")
        self.cancel_button.grid(row=0, column=1, sticky="e", padx=self.px((12, 0)))
        self.root.bind("<F5>", lambda event: self.query())
        self.root.bind("<Control-r>", lambda event: self.refresh_latest())
        self.root.bind("<Control-R>", lambda event: self.refresh_latest())
        self.root.bind("<Escape>", self._escape_pressed)
        self._query_controls()

    def _build_top_bar(self):
        top = ttk.LabelFrame(self.root, text="공통 설정 · 계정", padding=self.px((14, 8)))
        top.pack(side="top", fill="x", padx=self.px(18), pady=self.px((10, 0)))
        top.columnconfigure(1, weight=1)
        # The square logo fills the left of the title and gallery rows.
        self.logo_images, self.logo_size = {}, None
        self.brand_logo = ttk.Label(top)
        self.brand_logo.grid(row=0, column=0, rowspan=2, sticky="w", padx=self.px((0, 14)))
        header = ttk.Frame(top)
        header.grid(row=0, column=1, sticky="ew", pady=self.px((0, 6)))
        ttk.Label(header, text=APP_NAME, font=("맑은 고딕", 18, "bold")).pack(side="left")
        ttk.Label(header, text=APP_DESCRIPTION, font=("맑은 고딕", 10)).pack(side="left", padx=self.px(12))
        self.logout_button = ttk.Button(header, text="로그아웃", command=self.sign_out)
        self.logout_button.pack(side="right")
        self.other_account_button = ttk.Button(header, text="다른 계정 / 추가", command=self.new_account)
        self.other_account_button.pack(side="right", padx=self.px((0, 6)))
        # Defense is the urgent flow: start/stop and its state stay visible from every tab.
        # Settings (pages, new-member option) and the live numbers live in the defense tab.
        defense = ttk.Frame(header)
        defense.pack(side="right", padx=self.px((0, 24)))
        self.defense_state_label = ttk.Label(defense, textvariable=self.defense_state,
                                             font=("맑은 고딕", 10, "bold"), foreground=DEFENSE_OFF)
        self.defense_state_label.pack(side="left", padx=self.px((0, 10)))
        self.defense_button = ttk.Button(defense, text="방어 켜기", style="Accent.TButton", width=10,
                                         command=self.toggle_defense)
        self.defense_button.pack(side="left")
        context_row = ttk.Frame(top)
        context_row.grid(row=1, column=1, sticky="ew", pady=self.px((0, 4)))
        self.logo_rows = (header, context_row)
        for row in self.logo_rows:
            row.bind("<Configure>", self._fit_logo, add="+")
        query_scope = ttk.Frame(context_row)
        query_scope.pack(side="left")
        ttk.Label(query_scope, text="갤러리").pack(side="left", padx=self.px((0, 6)))
        self.gallery_box = ttk.Combobox(query_scope, textvariable=self.gallery,
                                        values=self._gallery_values(), width=24)
        self.gallery_box.pack(side="left")
        self.permission_button = ttk.Button(query_scope, text="관리 권한 확인", command=self.check_permission)
        self.permission_button.pack(side="left", padx=self.px(8))
        ttk.Label(query_scope, textvariable=self.permission_status).pack(side="left", padx=self.px((4, 0)))
        ttk.Label(context_row, textvariable=self.account_status).pack(side="right")
        # Inputs and options collapse after login so the post list gets the space.
        self.login_inputs = ttk.Frame(top)
        self.login_inputs.grid(row=2, column=0, columnspan=2, sticky="w", pady=self.px((4, 0)))
        ttk.Label(self.login_inputs, text="아이디").pack(side="left")
        self.account_entry = ttk.Combobox(self.login_inputs, textvariable=self.account_text, width=18)
        self.account_entry.pack(side="left", padx=self.px(8))
        ttk.Label(self.login_inputs, text="비밀번호").pack(side="left")
        self.password_entry = ttk.Entry(self.login_inputs, textvariable=self.password_text, show="●", width=20)
        self.password_entry.pack(side="left", padx=self.px(8))
        self.login_button = ttk.Button(self.login_inputs, text="로그인", command=self.sign_in)
        self.login_button.pack(side="left", padx=self.px(4))
        ttk.Label(self.login_inputs, text="저장 계정은 비밀번호를 비우면 저장 비밀번호로 로그인").pack(
            side="left", padx=self.px((8, 0)))
        self.login_options = ttk.Frame(top)
        self.login_options.grid(row=3, column=0, columnspan=2, sticky="w", pady=self.px((4, 0)))
        self.remember_box = ttk.Checkbutton(self.login_options, text="이 PC에 계정 저장",
                                            variable=self.remember_account)
        self.remember_box.pack(side="left")
        self.auto_box = ttk.Checkbutton(self.login_options, text="다음 실행 시 자동 로그인",
                                        variable=self.auto_login)
        self.auto_box.pack(side="left", padx=self.px(12))
        self.password_entry.bind("<KeyPress>", lambda event: self._cancel_auto_login())
        self.password_entry.bind("<Return>", lambda event: self.sign_in())

    def _fit_logo(self, event=None):
        # Natural row heights, not actual ones, so the logo never pushes the rows taller.
        header, context_row = self.logo_rows
        room = header.winfo_reqheight() + context_row.winfo_reqheight() + self.px(6) + self.px(4)
        size = max((s for s in LOGO_SIZES if s <= room), default=LOGO_SIZES[0])
        if size == self.logo_size:
            return
        if size not in self.logo_images:
            try:
                self.logo_images[size] = tk.PhotoImage(master=self.root, file=str(logo_path(size)))
            except tk.TclError:
                # A missing cosmetic asset must not prevent starting or closing work.
                return
        self.logo_size = size
        self.brand_logo.configure(image=self.logo_images[size])

    def _build_list_tab(self, frame):
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        toolbar = ttk.Frame(frame)
        toolbar.grid(row=0, column=0, sticky="ew", pady=self.px((0, 8)))
        nav = ttk.Frame(toolbar)
        nav.pack(side="left")
        # One click per common range; the entry and arrows below cover any other pages.
        self.range_buttons = []
        for label, pages in RANGES:
            button = ttk.Button(nav, text=label, command=lambda pages=pages: self.query_range(pages))
            button.pack(side="left", padx=self.px((0, 4)))
            self.range_buttons.append(button)
        self.latest_button = self.range_buttons[0]
        ttk.Label(nav, text="직접").pack(side="left", padx=self.px((12, 4)))
        self.previous_page_button = ttk.Button(nav, text="←", width=3, command=lambda: self.move_page(-1))
        self.previous_page_button.pack(side="left")
        self.pages_entry = ttk.Entry(nav, textvariable=self.pages, width=10)
        self.pages_entry.pack(side="left", padx=self.px(4))
        self.next_page_button = ttk.Button(nav, text="→", width=3, command=lambda: self.move_page(1))
        self.next_page_button.pack(side="left")
        self.query_button = ttk.Button(nav, text="조회", command=self.query)
        self.query_button.pack(side="left", padx=self.px((8, 0)))
        self.pages_entry.bind("<Return>", lambda event: self.query())
        pick_bar = ttk.Frame(toolbar)
        pick_bar.pack(side="right")
        self.selection_buttons = []
        for label, mode in (("비회원 일반글 선택", "guests"), ("일반글 전체 선택 (회원 포함)", "all"),
                            ("선택 해제", "none")):
            button = ttk.Button(pick_bar, text=label, command=lambda mode=mode: self.select(mode), state="disabled")
            button.pack(side="left", padx=self.px((0, 6)))
            self.selection_buttons.append(button)
        self.recommend_select_button = ttk.Button(
            pick_bar, text="개념글만 선택", command=lambda: self.select("recommended"), state="disabled")
        self.recommend_select_button.pack(side="left")
        self.selection_buttons.append(self.recommend_select_button)
        table = ttk.Frame(frame)
        table.grid(row=1, column=0, sticky="nsew")
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        columns = ("check", "id", "title", "author", "kind")
        self.tree = ttk.Treeview(table, columns=columns, show="headings", selectmode="browse")
        for name, label, width, stretch in zip(columns, ("선택", "글 번호", "제목", "작성자 구분", "글 종류"),
                                               (48, 90, 490, 170, 105), (False, False, True, False, False)):
            self.tree.heading(name, text=label)
            self.tree.column(name, width=self.px(width), minwidth=self.px(width if not stretch else 180),
                             stretch=stretch, anchor="w" if name == "title" else "center")
        # Row colours: later-configured tags win, so the uncertain style overrides the others.
        self.tree.tag_configure('guest', background='#FFF1DC')
        self.tree.tag_configure('recommended', background='#E6EEFD')
        self.tree.tag_configure('uncertain', foreground='#7A7A7A')
        self.tree.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        # The title column stretches, so the table never needs a sideways scrollbar.
        self.tree.configure(yscrollcommand=vertical.set)
        self.tree.bind("<Button-1>", self._click)
        self.tree.bind("<space>", self._space)
        self.tree.bind("<Delete>", self._delete_key)
        self.review_container = ttk.Frame(frame)
        self.review_container.grid(row=2, column=0, sticky="ew", pady=self.px((6, 0)))
        summary_row = ttk.Frame(frame)
        summary_row.grid(row=3, column=0, sticky="ew", pady=self.px((8, 2)))
        ttk.Label(summary_row, textvariable=self.count, font=("맑은 고딕", 10, "bold")).pack(side="left")
        ttk.Label(summary_row, text="  ·  주황: 비회원 · 파랑: 개념글 · 회색: 판별 불가 (체크칸 클릭 / Space)").pack(
            side="left")
        ttk.Label(frame, textvariable=self.ids, wraplength=self.px(1000)).grid(
            row=4, column=0, sticky="w", pady=self.px((0, 4)))
        # Two rows so the hint and every action button fit at high display scaling.
        hint_box = ttk.Frame(frame)
        hint_box.grid(row=5, column=0, sticky="ew", pady=self.px((4, 0)))
        self.keep_recommended_button = ttk.Button(
            hint_box, text="개념글만 남기기", command=lambda: self.filter_selected("recommended"), state="disabled")
        self.keep_recommended_button.pack(side="right")
        self.keep_ordinary_button = ttk.Button(
            hint_box, text="일반글만 남기기", command=lambda: self.filter_selected("ordinary"), state="disabled")
        self.keep_ordinary_button.pack(side="right", padx=self.px((8, 4)))
        self.selection_hint_label = ttk.Label(hint_box, textvariable=self.selection_hint, foreground="#9A3412",
                                              wraplength=self.px(700))
        self.selection_hint_label.pack(side="left", fill="x", expand=True)
        action_bar = ttk.Frame(frame)
        action_bar.grid(row=6, column=0, sticky="ew", pady=self.px((6, 0)))
        actions_right = ttk.Frame(action_bar)
        actions_right.pack(side="right")
        self.send_to_bump_button = ttk.Button(
            actions_right, text="끌올로 보내기", command=self.send_selected_to_bump, state="disabled")
        self.send_to_bump_button.pack(side="left", padx=self.px((0, 6)))
        self.review_button = ttk.Button(
            actions_right, text="선택한 글 확인", command=self.show_review, state="disabled")
        self.review_button.pack(side="left", padx=self.px((0, 6)))
        self.release_button = ttk.Button(
            actions_right, text="선택 개념글 해제", command=self.prepare_release, state="disabled")
        self.release_button.pack(side="left", padx=self.px((0, 6)))
        self.delete_button = ttk.Button(
            actions_right, text="선택 삭제", style="Accent.TButton", command=self.prepare_delete, state="disabled")
        self.delete_button.pack(side="left")

    def _delete_key(self, event=None):
        """Delete key = the delete button: same enable rule, same confirm step."""
        self.delete_button.invoke()
        return 'break'

    def _escape_pressed(self, *_):
        if self.delete_phase in {'ready', 'recommend_ready'}:
            self.cancel_delete()
        elif self.review_window is not None:
            self._clear_review()

    def _on_tab_changed(self, *_):
        if self.closed:
            return
        selected = self.notebook.select()
        if self.delete_phase in {'ready', 'executing', 'recommend_ready', 'recommend_executing'}:
            if selected != str(self.list_tab):
                self.notebook.select(self.list_tab)
            return
        if selected == str(self.defense_tab):
            self._ensure_defense_window()
        elif selected == str(self.bump_tab):
            self._ensure_bump_window()

    # ------------------------------------------------------------- state table
    def _delete_state(self, phase):
        if phase not in PHASES:
            raise ValueError(f'알 수 없는 화면 상태: {phase}')
        self.delete_phase = phase
        self._apply_controls()

    def _apply_controls(self):
        """Derive every control's state from (phase, login, busy). Single source of truth."""
        phase = self.delete_phase
        active = phase is not None
        authenticated = self.login_result is not None
        idle = 'disabled' if active else 'normal'
        for widget in (self.gallery_box, self.pages_entry):
            widget.configure(state=idle)
        # During defense the same button stops it; any other job keeps it locked.
        self.defense_button.configure(state='normal' if phase in (None, 'defense') else 'disabled',
                                      text='방어 끄기' if phase == 'defense' else '방어 켜기')
        for widget in (self.account_entry, self.password_entry, self.remember_box,
                       self.auto_box, self.login_button):
            widget.configure(state='disabled' if active or authenticated else 'normal')
        self.other_account_button.configure(state=idle)
        signed_in_idle = 'normal' if authenticated and not active else 'disabled'
        self.logout_button.configure(state=signed_in_idle)
        self.permission_button.configure(state=signed_in_idle)
        self.cancel_button.configure(state='normal' if phase in CANCELLABLE else 'disabled')
        for button in self.selection_buttons:
            button.configure(state='normal' if self.model.rows and not active else 'disabled')
        if authenticated:
            self.login_inputs.grid_remove()
            self.login_options.grid_remove()
        else:
            self.login_inputs.grid()
            self.login_options.grid()
        if self.defense_window is not None and not getattr(self.defense_window, 'closed', True):
            if phase not in {None, 'defense'}:
                self.defense_window._enabled(False)
            elif phase is None and (self.defense_window.task is None or not self.defense_window.task.alive):
                self.defense_window._enabled(True)
        if self.bump_window is not None and not getattr(self.bump_window, 'closed', True):
            if phase not in {None, 'bump'}:
                self.bump_window._enabled(False)
            elif phase is None and (self.bump_window.task is None or not self.bump_window.task.alive):
                self.bump_window._enabled(True)
        self._query_controls()
        self._refresh_selection()

    def _query_controls(self):
        locked = self.closed or self.busy or bool(self.delete_phase)
        state = 'disabled' if locked else 'normal'
        self.query_button.configure(state=state)
        for button in self.range_buttons:
            button.configure(state=state)
        try:
            pages = parse_pages(self.pages.get())
        except ValueError:
            pages = ()
        self.previous_page_button.configure(state='normal' if not locked and pages and min(pages) > 1 else 'disabled')
        self.next_page_button.configure(state='normal' if not locked and pages and max(pages) < 1_000_000 else 'disabled')

    # ------------------------------------------------------- gallery store
    def _gallery_values(self):
        builtin_ids = set(GALLERIES.values())
        extras = tuple(g for g in self.recent_galleries if g not in builtin_ids)
        return (*GALLERIES.keys(), *extras)

    def _load_galleries(self):
        if self.gallery_store is None:
            return
        try:
            self.recent_galleries = self.gallery_store.load()
            self.gallery_box.configure(values=self._gallery_values())
            if not self.gallery.get() and self.recent_galleries:
                self.gallery.set(self.recent_galleries[0])
        except Exception:
            record_error('manager_ui._load_galleries')

    def _remember_gallery(self, gallery):
        if self.gallery_store is None:
            return
        try:
            if self.gallery_store.remember(gallery):
                self.recent_galleries = self.gallery_store.load()
                self.gallery_box.configure(values=self._gallery_values())
        except Exception:
            record_error('manager_ui._remember_gallery')

    # ------------------------------------------------------- browsing & query
    def _clear_review(self):
        if self.review_window is not None:
            self.review_window.destroy()
            self.review_window = None
            # Tk keeps an emptied frame at its old size; ask for a tiny one so the table grows back.
            self.review_container.configure(height=1)

    def _scope_changed(self, *_):
        if self.delete_phase:
            return
        self.permission_status.set('관리 삭제 권한 미확인 · 갤러리 범위 변경됨')
        self.cancel.set()
        self.model.invalidate()
        self._clear_review()
        self._render()
        self.status.set("범위 변경됨 · 다시 조회 필요" + (" (이전 조회 종료 대기)" if self.busy else ""))
        self.detail.set("기존 목록과 선택을 초기화함")
        self._query_controls()
        if self.defense_window is not None and not getattr(self.defense_window, 'closed', True):
            try:
                text = self.gallery.get().strip().lower()
                gallery = validate_gallery(GALLERIES.get(text, text))
                self.defense_window.gallery = gallery
                if hasattr(self.defense_window, 'scope_label'):
                    self.defense_window.scope_label.configure(
                        text=f'갤러리: {gallery} · 최신 페이지부터 감시할 페이지 수')
            except ValueError:
                pass

    def refresh_latest(self):
        self.query_range('1')

    def query_range(self, pages):
        if self.closed or self.busy or self.delete_phase:
            return
        self.pages.set(pages)
        self.query()

    def move_page(self, offset):
        if self.closed or self.busy or self.delete_phase or offset not in (-1, 1):
            return
        try:
            pages = parse_pages(self.pages.get())
            text = ', '.join(str(page + offset) for page in pages)
            parse_pages(text)
        except ValueError:
            return
        self.pages.set(text)
        self.query()

    def query(self):
        if self.closed or self.busy or self.delete_phase:
            return
        self._clear_review()
        self.model.invalidate()
        self._render()
        try:
            gallery_text = self.gallery.get().strip().lower()
            gallery = validate_gallery(GALLERIES.get(gallery_text, gallery_text))
            pages = parse_pages(self.pages.get())
            generation = self.model.begin(gallery, pages)
        except ValueError as error:
            self.status.set(str(error))
            self.detail.set("조회하지 않음 · 갤러리 ID와 페이지 입력 확인 필요")
            return
        self.busy = True
        self.cancel = Event()
        self._query_controls()
        self.status.set("공개 목록 조회 중…")
        self.detail.set(f"{gallery} · 페이지 {', '.join(map(str, pages))}")
        Thread(target=self._fetch, args=(generation, gallery, pages, self.cancel), daemon=True).start()

    def _fetch(self, generation, gallery, pages, cancel):
        from .public_board import PublicReadError

        def reader(gallery, page):
            if cancel.is_set():
                raise PublicReadError("조회 취소")
            return self.reader(gallery, page)
        try:
            result = preview_pages(gallery, "ui_preview", pages, reader=reader)
        except Exception:
            record_error('manager_ui._fetch')
            result = None  # Do not expose arbitrary server/exception text to the UI.
        self.messages.put((generation, result))

    def _read_auto_refresh(self, gallery, pages, cancel):
        from .public_board import PublicReadError

        def reader(g, p):
            if cancel.is_set():
                raise PublicReadError("조회 취소")
            return self.reader(g, p)
        try:
            result = preview_pages(gallery, "ui_preview", pages, reader=reader)
            if not any(page["status"] == "read" for page in result["page_results"]):
                return None
            return result
        except AssertionError:
            return None
        except Exception:
            record_error('manager_ui._read_auto_refresh')
            return None

    def _poll_query(self):
        try:
            generation, result = self.messages.get_nowait()
        except Empty:
            return
        self.busy = False
        self._query_controls()
        if generation != self.model.generation:
            self.status.set("이전 조회 종료됨 · 변경한 범위로 다시 조회 필요")
            return
        try:
            if result is None:
                raise ValueError("조회 실패")
            self.model.apply(generation, result)
            complete = self.model.complete
            read_count = sum(page["status"] == "read" for page in result["page_results"])
            if read_count:
                self._remember_gallery(self.model.gallery_id)
            state = "조회 완료" if complete else ("일부 조회 · 나머지 실패" if read_count else "조회 실패")
            self.status.set(f"{state} · {read_count}/{len(self.model.pages)}페이지 · {result['elapsed_ms']:.0f} ms")
            labels = {"read": "완료", "failed": "실패", "not_requested": "미조회"}
            server_down = any(p.get("reason") == "server_error" for p in result["page_results"])
            self.detail.set(" / ".join(f"{p['page']}페이지 {labels[p['status']]}" for p in result["page_results"])
                            + (" · 디시 서버가 응답하지 않음(서버 장애) · 잠시 뒤 다시 조회하세요" if server_down
                               else " · 조회 중 새 글이 밀리면 누락될 수 있음"))
        except (ValueError, KeyError, TypeError):
            self.model.invalidate()
            self.status.set("조회 실패 · 다시 조회하거나 입력 범위를 확인하세요")
            self.detail.set("목록을 표시할 수 없어 선택을 초기화함")
        self._render()

    def _render(self):
        children = self.tree.get_children()
        if children:
            self.tree.delete(*children)
        for post_id, row in self.model.rows.items():
            tags = []
            if row.disposition == 'delete_guest':
                tags.append('guest')
            if row.post_kind == 'recommended':
                tags.append('recommended')
            if row.disposition not in KNOWN_AUTHORS or row.post_kind not in {'ordinary', 'recommended'}:
                tags.append('uncertain')
            self.tree.insert("", "end", iid=str(post_id), tags=tuple(tags), values=(
                "☐", post_id, " ".join(row.title.split()), AUTHORS[row.disposition], KINDS[row.post_kind]))
        for button in self.selection_buttons:
            button.configure(state="normal" if self.model.rows and not self.delete_phase else "disabled")
        self._refresh_selection()

    def _refresh_selection(self):
        for post_id in self.model.rows:
            self.tree.set(str(post_id), "check", "☑" if post_id in self.model.selected else "☐")
        selected = self.model.review().rows
        guests = sum(row.disposition == "delete_guest" for row in selected)
        members = sum(row.disposition == "protected_member" for row in selected)
        unknown = len(selected) - guests - members
        self.count.set(f"조회 {len(self.model.rows)}개  |  선택 {len(selected)}개 (비회원 {guests} · 회원 {members} · 불명 {unknown})")
        numbers = ", ".join(str(row.post_id) for row in selected[:12])
        if len(selected) > 12:
            numbers += f" 외 {len(selected) - 12}개 · 전체 번호는 ‘선택한 글 확인’"
        self.ids.set("선택한 글 번호: " + (numbers or "없음"))
        idle_selected = bool(selected) and not self.busy and not self.delete_phase
        self.review_button.configure(state="normal" if selected and not self.delete_phase else "disabled")
        self.send_to_bump_button.configure(
            state="normal" if idle_selected and 1 <= len(selected) <= MAX_BUMP_TARGETS else "disabled")
        available = idle_selected and self.model.complete
        known = all(row.disposition in KNOWN_AUTHORS for row in selected)
        kinds = {row.post_kind for row in selected}
        self.delete_button.configure(state='normal' if available and known and kinds == {'ordinary'} else 'disabled')
        self.release_button.configure(state='normal' if available and known and kinds == {'recommended'} else 'disabled')
        has_ordinary = any(r.post_kind == 'ordinary' and r.disposition in KNOWN_AUTHORS for r in selected)
        has_recommended = any(r.post_kind == 'recommended' and r.disposition in KNOWN_AUTHORS for r in selected)
        needs_filter = idle_selected and (not known or len(kinds) > 1)
        self.keep_ordinary_button.configure(state='normal' if needs_filter and has_ordinary else 'disabled')
        self.keep_recommended_button.configure(state='normal' if needs_filter and has_recommended else 'disabled')
        self.selection_hint.set(self._selection_hint(selected, known, kinds))

    def _selection_hint(self, rows, known, kinds):
        """Say why delete/release is (un)available instead of silently disabling the buttons."""
        if not rows or self.delete_phase or self.busy:
            return ''
        if not self.model.complete:
            return '일부 페이지만 조회됨 · 전체를 다시 조회해야 삭제·해제할 수 있음'
        if not known:
            return '판별 불가·정보 충돌 글이 섞여 있음 · 그 글을 빼고 고르세요'
        if len(rows) > MAX_MANUAL_TARGETS:
            return f'한 번에 최대 {MAX_MANUAL_TARGETS}개까지 처리 가능 · 선택을 줄이세요'
        if kinds == {'ordinary'}:
            return f'일반글 {len(rows)}개 선택됨 · ‘선택 삭제’ 가능'
        if kinds == {'recommended'}:
            return f'개념글 {len(rows)}개 선택됨 · ‘선택 개념글 해제’ 가능'
        if {'ordinary', 'recommended'} <= kinds:
            return '일반글과 개념글이 섞여 있음 · 삭제는 일반글만, 개념글 해제는 개념글만 골라야 함'
        return '종류를 알 수 없는 글이 섞여 있음 · 그 글을 빼고 고르세요'

    def select(self, mode):
        if self.closed or self.busy or self.delete_phase:
            return
        self._clear_review()
        if mode == 'recommended':
            self.model.selected = {pid for pid, row in self.model.rows.items()
                                   if row.post_kind == 'recommended' and row.disposition in KNOWN_AUTHORS}
        else:
            self.model.select(mode)
            # Bulk buttons pick deletable ordinary posts only; recommended posts have their own button.
            self.model.selected.intersection_update(
                pid for pid, row in self.model.rows.items()
                if row.post_kind == 'ordinary' and row.disposition in KNOWN_AUTHORS)
        self._refresh_selection()

    def filter_selected(self, kind):
        if self.closed or self.busy or self.delete_phase or kind not in {'ordinary', 'recommended'}:
            return
        self._clear_review()
        self.model.selected = {
            pid for pid in self.model.selected
            if pid in self.model.rows
            and self.model.rows[pid].post_kind == kind
            and self.model.rows[pid].disposition in KNOWN_AUTHORS
        }
        self._refresh_selection()

    def send_selected_to_bump(self):
        if self.closed or self.busy or self.delete_phase:
            return
        review = self.model.review()
        if not (1 <= len(review.rows) <= MAX_BUMP_TARGETS):
            self.status.set('끌올로 보낼 글 1~20개를 먼저 선택하세요')
            return
        try:
            urls = [BumpTarget(review.gallery_id, row.post_id).url for row in review.rows]
        except ValueError:
            self.status.set('갤러리 ID 또는 글 번호 확인 필요')
            return
        window = self._ensure_bump_window()
        if window is None:
            return
        if len(urls) == 1:
            window.set_experimental(False)
            window.url.set(urls[0])
            window.second_url.set('')
            window.additional_urls.delete('1.0', 'end')
            window.status.set('목록에서 글 1개 주소가 채워짐 · 1회 시험 또는 반복 시작 가능')
        else:
            window.set_experimental(True)
            window.url.set(urls[0])
            window.second_url.set(urls[1])
            window.additional_urls.delete('1.0', 'end')
            if len(urls) > 2:
                window.additional_urls.insert('1.0', '\n'.join(urls[2:]))
            window.status.set(f'목록에서 글 {len(urls)}개 주소가 채워짐 · 전체 1회씩 또는 반복 시작 가능')
        self.notebook.select(self.bump_tab)
        self.status.set(f'선택한 글 {len(urls)}개 주소를 끌올 탭에 채움 · 요청은 보내지 않음')

    def _toggle(self, iid):
        if self.delete_phase:
            return
        if iid:
            self._clear_review()
            self.model.toggle(int(iid))
            self._refresh_selection()

    def _click(self, event):
        if self.tree.identify_region(event.x, event.y) == "cell" and self.tree.identify_column(event.x) == "#1":
            iid = self.tree.identify_row(event.y)
            if iid:
                self.tree.focus(iid)
                self.tree.selection_set(iid)
                self.tree.focus_set()
                self._toggle(iid)
            return "break"

    def _space(self, event):
        self._toggle(self.tree.focus())
        return "break"

    def show_review(self):
        if self.delete_phase:
            return
        review = self.model.review()
        if not review.rows:
            return
        header = f"갤러리: {review.gallery_id}\n페이지: {', '.join(map(str, review.pages))}\n선택: {len(review.rows)}개"
        if not review.complete:
            header += "\n일부 조회 결과임 · 조회되지 않은 페이지는 포함되지 않음"
        lines = [f"{row.post_id} | {AUTHORS[row.disposition]} | {KINDS[row.post_kind]} | {' '.join(row.title.split())}"
                 for row in review.rows]
        body = header + "\n\n" + "\n".join(lines) + "\n\n확인용 목록이며 삭제 요청은 전송되지 않음"
        self._delete_dialog("선택한 글 확인 — 삭제되지 않음", body, confirm=False)

    def _delete_dialog(self, title, body, *, confirm=False):
        self._clear_review()
        window = self.review_window = ttk.LabelFrame(self.review_container, text=title, padding=self.px(8))
        window.pack(fill='x', expand=True)
        window.title = lambda *args: title
        window.protocol = lambda *args: None
        window.transient = lambda *args: None
        px = lambda value: pixels(window, value)
        footer = ttk.Frame(window, padding=px((4, 6, 4, 0)))
        footer.pack(side='bottom', fill='x')
        if confirm:
            self.confirm_delete_button = ttk.Button(footer, text='위 글 실제 삭제', command=self.confirm_delete)
            self.confirm_delete_button.pack(side='right')
            ttk.Button(footer, text='취소', command=self.cancel_delete).pack(side='right', padx=px(10))
        else:
            ttk.Button(footer, text='닫기', command=self._clear_review).pack(side='right')
        text = tk.Text(window, wrap='word', font=('맑은 고딕', 10), padx=px(16), pady=px(12), height=6)
        scroll = ttk.Scrollbar(window, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        text.pack(fill='both', expand=True)
        text.insert('1.0', body)
        text.configure(state='disabled')
        monitor_window(window)

    # ------------------------------------------------------------------ account
    def _cancel_auto_login(self):
        if self.auto_login_id is not None:
            self.root.after_cancel(self.auto_login_id)
            self.auto_login_id = None

    def _account_changed(self, *_):
        self._cancel_auto_login()
        self.password_text.set('')
        self.use_saved.set(self.account_text.get().strip().lower() in self.profiles.accounts)

    def _password_changed(self, *_):
        if self.password_text.get():
            self._cancel_auto_login()
            self.use_saved.set(False)
        else:
            self.use_saved.set(self.account_text.get().strip().lower() in self.profiles.accounts)

    def _load_profiles(self, *, startup=False):
        if self.profile_store is None:
            return
        try:
            self.profiles = self.profile_store.load()
            self.account_entry.configure(values=self.profiles.accounts)
            self.use_saved.set(
                (not bool(self.password_text.get()))
                and (self.account_text.get().strip().lower() in self.profiles.accounts))
            if startup and self.profiles.last_account:
                self.account_text.set(self.profiles.last_account)
                self.auto_login.set(self.profiles.auto_login)
                if self.profiles.auto_login:
                    self.auto_login_id = self.root.after(350, self._startup_login)
        except Exception:
            record_error('manager_ui._load_profiles')
            self.account_status.set('저장 계정 목록 확인 실패 · 아이디와 비밀번호로 직접 로그인 가능')

    def _startup_login(self):
        self.auto_login_id = None
        if self.closed or self.close_requested or not self.auto_login.get():
            return
        self.sign_in(automatic=True)

    def _disable_auto_login(self):
        return disable_auto_login(self.profile_store)

    def new_account(self):
        if self.closed or self.busy or self.delete_phase:
            return
        self.sign_out()
        self.account_text.set('')
        self.remember_account.set(True)
        self.auto_login.set(True)
        self.account_entry.focus_set()

    def sign_in(self, *, automatic=False):
        if self.closed or self.busy or self.delete_phase or self.login_result is not None:
            return
        self._cancel_auto_login()
        self.automatic_attempt = automatic
        password = self.password_text.get()
        self.password_text.set('')
        use_saved = (not bool(password)) and self.use_saved.get()
        try:
            options = {}
            if self.profile_store is not None and self.remember_account.get():
                options['on_verified'] = remember_after_login(
                    self.profile_store, auto_login=self.auto_login.get(), write_password=not use_saved)
            elif self.profile_store is not None and not self._disable_auto_login():
                self.account_status.set('자동 로그인 설정 저장 실패 · 로그인 중단')
                return
            self.login_task = self.login_task_factory(self.account_text.get(), password,
                                                       use_saved=use_saved, **options)
        except Exception:
            record_error('manager_ui.sign_in')
            self.account_status.set('아이디·비밀번호 입력 확인 필요')
            return
        finally:
            password = None
        self._delete_state('login')
        self.account_status.set('로그인 중 · 보안문자가 나오면 직접 입력')
        self.login_task.start()

    def sign_out(self):
        if self.delete_phase or self.busy:
            return
        self._cancel_auto_login()
        saved = self._disable_auto_login()
        self.auto_login.set(False)
        result, self.login_result = self.login_result, None
        close_login(result)
        self.password_text.set('')
        self.account_status.set('로그아웃됨 · 저장 계정을 선택하거나 다른 계정 추가 가능' if saved else
                               '로그아웃됨 · 자동 로그인 해제 저장 실패, 다음 실행 시 재로그인할 수 있음')
        self.permission_status.set('관리 삭제 권한 미확인')
        if self.bump_window is not None and not getattr(self.bump_window, 'closed', True):
            self.bump_window.set_borrowed_session(None)
        if self.defense_window is not None and not getattr(self.defense_window, 'closed', True):
            self.defense_window.account = ''
        self.model.invalidate()
        self._delete_state(None)
        self._render()

    def _require_login(self):
        if self.login_result is None:
            self.status.set('먼저 본인 관리 계정으로 로그인 필요함')
            messagebox.showwarning('로그인 필요',
                '이 기능을 사용하려면 먼저 관리 계정으로 로그인하세요.\n\n'
                '위쪽 계정 영역에서 저장 계정을 선택하거나 아이디·비밀번호를 입력한 뒤 로그인하세요.',
                parent=self.root)
            self.account_entry.focus_set()
            return False
        return True

    def _scope(self):
        text = self.gallery.get().strip().lower()
        return validate_gallery(GALLERIES.get(text, text)), parse_pages(self.pages.get())

    def check_permission(self):
        if self.closed or self.busy or self.delete_phase or not self._require_login():
            return
        try:
            gallery, pages = self._scope()
            adapter = ListDefenseAdapter(self.login_result, gallery, pages=pages, max_age=60)
        except Exception:
            record_error('manager_ui.check_permission')
            self.permission_status.set('갤러리·페이지 입력 확인 필요')
            return
        self.delete_cancel = Event()
        self._delete_state('permission')
        self.permission_status.set(f'{gallery} 관리 삭제 권한 확인 중')
        Thread(target=self._permission_worker, args=(adapter, self.delete_cancel), daemon=False).start()

    def _permission_worker(self, adapter, cancel):
        reason = None
        try:
            adapter.read_snapshot(cancel)
        except DefenseReadError as error:
            reason = str(error)
        except Exception:
            record_error('manager_ui._permission_worker')
            reason = 'worker_failed'
        self.delete_messages.put(('permission', (adapter.gallery, reason)))

    # ----------------------------------------------------- recommendation release
    def prepare_release(self):
        if self.closed or self.busy or self.delete_phase or not self._require_login():
            return
        review = self.model.review()
        try:
            self.prepared = self.release_factory(self.login_result, review)
        except Exception:
            record_error('manager_ui.prepare_release')
            self.status.set('완료된 조회에서 회원·비회원 개념글 1~250개를 선택해야 함')
            return
        self.delete_review = review
        self.delete_cancel = Event()
        self._delete_state('recommend_ready')
        body = (f'계정: {self.login_result.verified_user_id}\n갤러리: {review.gallery_id}\n'
                f'선택한 개념글 {len(review.rows)}개 · 회원 포함\n'
                '게시글·댓글·추천 수는 그대로 두고 개념글 등록 상태만 해제함.\n\n')
        body += '\n'.join(f'{row.post_id} | {AUTHORS[row.disposition]} | {row.title}' for row in review.rows)
        body += ('\n\n해제 요청은 한 번만 전송함. 일부만 반영될 수 있으며 자동 재전송하지 않음.'
                 '\n실행 후 같은 페이지를 다시 읽어 확인함. 페이지 밖으로 이동한 글은 미확인으로 표시함.')
        self.status.set('개념글 해제 확인 대기 · 아직 요청하지 않음')
        self._delete_dialog('선택 개념글 해제 확인', body, confirm=True)
        self.confirm_delete_button.configure(text='위 글 개념글 해제', command=self.confirm_release)

    def confirm_release(self):
        if self.closed or self.delete_phase != 'recommend_ready' or self.prepared is None:
            return
        action, self.prepared = self.prepared, None
        review = self.delete_review
        self._clear_review()
        self._delete_state('recommend_executing')
        self.status.set('현재 개념글·권한 확인 후 해제 실행 중')
        self.detail.set('이미 보낸 요청은 취소할 수 없음. 완료 후 목록 상태를 확인함.')
        Thread(target=self._release_worker, args=(action, review, self.delete_cancel), daemon=False).start()

    def _release_worker(self, action, review, cancel):
        try:
            receipt = action.execute(cancel)
        except Exception:
            record_error('manager_ui._release_worker')
            receipt = None
        refreshed = None
        if not cancel.is_set() and review is not None:
            refreshed = self._read_auto_refresh(review.gallery_id, review.pages, cancel)
        self.delete_messages.put(('recommendation', (receipt, refreshed)))

    def _release_result(self, receipt, refreshed=None):
        self.model.invalidate()
        if refreshed is not None and self.delete_review is not None and not self.close_requested:
            try:
                gen = self.model.begin(self.delete_review.gallery_id, self.delete_review.pages)
                self.model.apply(gen, refreshed)
            except (ValueError, KeyError, TypeError):
                self.model.invalidate()
        self._render()
        self._delete_state(None)
        summary = format_release_result(receipt) if receipt is not None else '해제 결과 미확인'
        labels = {**REASONS, 'selection_changed': '글이 이동했거나 제목·작성자·개념글 상태가 변경됨',
                  'release_control_unverified': '개념글 해제 관리 버튼을 확인하지 못함',
                  'already_used': '이미 실행한 선택임'}
        if receipt is not None and receipt.stopped:
            summary += '\n' + labels.get(receipt.stopped, '목록 재확인 필요')
        summary += '\n\n방금 본 페이지를 다시 조회해 현재 남은 목록을 표에 반영함. 자동 재전송하지 않음.'
        self.status.set('개념글 해제 처리 종료')
        self.detail.set(summary.split('\n')[0])
        if not self.close_requested:
            self._delete_dialog('개념글 해제 결과', summary)

    # ------------------------------------------------------------ manual delete
    def prepare_delete(self):
        if self.closed or self.busy or self.delete_phase or not self._require_login():
            return
        review = self.model.review()
        try:
            validate_review(review)
            self.prepared = self.action_factory(self.login_result, review)
        except Exception:
            record_error('manager_ui.prepare_delete')
            self.status.set('완료된 조회에서 일반글 1~250개를 선택해야 함')
            return
        self.delete_review = review
        self.delete_cancel = Event()
        self._delete_state('ready')
        members = sum(row.disposition == 'protected_member' for row in review.rows)
        body = (f'계정: {self.login_result.verified_user_id}\n갤러리: {review.gallery_id}\n'
                f'선택 {len(review.rows)}개 · 회원 {members}개 포함\n'
                '요청당 최대 5글 · 동시 최대 12요청 · 일부만 처리된 뒤 중지될 수 있음\n\n')
        body += '\n'.join(f'{r.post_id} | {AUTHORS[r.disposition]} | {r.title}' for r in review.rows)
        body += ('\n\n게시글과 댓글의 원래 게시 상태는 복구할 수 없음.'
                 '\n실행 직전 계정·관리 권한·현재 목록을 다시 확인함. 공지·판별 불가 글은 제외함.')
        self.status.set('선택 삭제 확인 대기 · 아직 요청하지 않음')
        self._delete_dialog('선택 삭제 확인', body, confirm=True)

    def confirm_delete(self):
        if self.closed or self.delete_phase != 'ready' or self.prepared is None:
            return
        action, self.prepared = self.prepared, None
        review = self.delete_review
        if isinstance(getattr(action, 'performance', None), ManualPerformance):
            action.performance.start()
        self._clear_review()
        self._delete_state('executing')
        self.status.set('현재 목록 확인 후 삭제 실행 중')
        self.detail.set('중지는 후속 요청을 막음. 이미 보낸 요청의 결과는 통신 종료 후 표시함.')
        Thread(target=self._manual_worker, args=(action, review, self.delete_cancel), daemon=False).start()

    def _manual_worker(self, action, review, cancel):
        receipt, reason = None, None
        try:
            receipt = action.execute(cancel)
        except DefenseReadError as error:
            reason = str(error)
        except Exception:
            record_error('manager_ui._manual_worker')
            reason = 'preparation_failed'
        performance = getattr(action, 'performance', None)
        refreshed = None
        if not cancel.is_set() and review is not None:
            refreshed = self._read_auto_refresh(review.gallery_id, review.pages, cancel)
        self.delete_messages.put(('manual', (receipt, reason, performance, refreshed)))

    def cancel_delete(self):
        if self.delete_phase == 'login':
            self.login_task.stop()
            self.account_status.set('로그인 중지 대기')
        elif self.delete_phase in {'ready', 'recommend_ready'}:
            release = self.delete_phase == 'recommend_ready'
            self.prepared = None
            self._clear_review()
            self._delete_state(None)
            self.status.set(('개념글 해제' if release else '선택 삭제') + ' 취소됨 · 요청 없음')
        elif self.delete_phase in {'executing', 'permission', 'recommend_executing'}:
            self.delete_cancel.set()
            self.status.set('중지 대기 · 이미 시작한 통신 종료 후 완료됨')

    def _poll_delete(self):
        try:
            kind, payload = self.delete_messages.get_nowait()
        except Empty:
            return
        if kind == 'recommendation':
            if isinstance(payload, tuple):
                self._release_result(*payload)
            else:
                self._release_result(payload)
        elif kind == 'permission':
            gallery, reason = payload
            self._delete_state(None)
            prefix = '목록 조회 실패 · ' if reason in {'empty_response', 'server_error'} else '권한 확인 실패 · '
            self.permission_status.set(f'{gallery} 관리 삭제 권한 확인됨' if reason is None else
                                       prefix + REASONS.get(reason, '다시 확인 필요'))
        elif kind == 'manual':
            self._manual_result(*payload)

    def _manual_result(self, receipt, reason, performance, refreshed=None):
        self.model.invalidate()
        if refreshed is not None and self.delete_review is not None and not self.close_requested:
            try:
                gen = self.model.begin(self.delete_review.gallery_id, self.delete_review.pages)
                self.model.apply(gen, refreshed)
            except (ValueError, KeyError, TypeError):
                self.model.invalidate()
        self._render()
        self._delete_state(None)
        if isinstance(receipt, ManualReceipt):
            summary = format_manual_result(receipt)
            reason = receipt.stopped
            if receipt.result_counts['accepted']:
                self.permission_status.set(f'{self.delete_review.gallery_id} 관리 삭제 요청 수락됨')
        elif receipt is not None and receipt.response is BatchResponse.SUCCESS_REPORTED:
            summary = f'서버가 선택 {len(self.delete_review.rows)}개 삭제 요청을 수락함. 글별 삭제 완료 수는 미확정임.'
            self.permission_status.set(f'{self.delete_review.gallery_id} 관리 삭제 요청 수락됨')
        elif receipt is not None and receipt.attempted:
            summary = ('서버가 삭제 요청을 거부함' if receipt.response is BatchResponse.REJECTION_REPORTED else
                       '요청 전송 후 응답을 확정하지 못함 · 일부 또는 전체 삭제되었을 수 있음')
            reason = receipt.stopped
        else:
            summary = '삭제 요청 없음'
            reason = reason or (receipt.stopped if receipt else None)
        labels = {**REASONS, 'selection_changed': '글이 이동했거나 제목·작성자·종류가 변경됨',
                  'preparation_failed': '대상 확인 또는 요청 처리 실패', 'already_used': '이미 실행한 선택임'}
        if reason:
            summary += '\n' + labels.get(reason, '다시 확인 필요')
        if reason in {'auth_required', 'forbidden'}:
            self.permission_status.set('계정 또는 관리 권한 다시 확인 필요')
        summary += '\n\n방금 본 페이지를 다시 조회해 현재 남은 글을 표에 반영함. 자동 재전송하지 않음.'
        self.status.set('선택 삭제 처리 종료')
        self.detail.set(summary.split('\n')[0])
        if not self.close_requested:
            self._delete_dialog('선택 삭제 결과', summary)
        if isinstance(performance, ManualPerformance):
            performance.mark_ui_ready()
            elapsed = performance.summary()['ui_ready_ms']
            saved = performance.save()
            label = '진단 로그 저장됨' if saved else '진단 로그 저장 실패 · 삭제 결과는 위와 같음'
            self.status.set(f'선택 삭제 처리 종료 · {elapsed / 1000:.2f}초 · {label}')

    # ------------------------------------------------------------------ defense
    def _make_defense_factory(self):
        if self.defense_task_factory is not None:
            return self.defense_task_factory
        return lambda gallery, pages, **options: DefenseTask(
            gallery, pages, login_result=self.login_result, **options)

    def _before_defense_start(self):
        if self.closed or self.busy or self.delete_phase is not None or not self._require_login():
            return False
        try:
            text = self.gallery.get().strip().lower()
            gallery = validate_gallery(GALLERIES.get(text, text))
        except ValueError:
            self.status.set('갤러리 입력 확인 필요')
            return False
        if self.defense_window is not None:
            self.defense_window.gallery = gallery
            self.defense_window.account = self.login_result.verified_user_id
            self.defense_window.task_factory = self._make_defense_factory()
        return True

    def toggle_defense(self):
        """Top-bar button: start defense with the defense tab's settings, or stop it."""
        if self.closed:
            return
        if self.delete_phase == 'defense':
            if self.defense_window is not None and not getattr(self.defense_window, 'closed', True):
                self.defense_window.stop()
            return
        if self.busy and not self.delete_phase:
            self.status.set('목록 조회가 끝난 뒤 방어를 켜세요')
            return
        if self.delete_phase or not self._require_login():
            return
        try:
            window = self._ensure_defense_window()
        except Exception:
            record_error('manager_ui.toggle_defense')
            self.defense_window = None
            self.status.set('방어를 시작하지 못함 · 요청 없음')
            return
        window.start()

    def _show_defense_state(self, text, running):
        self.defense_state.set(text)
        self.defense_state_label.configure(foreground=DEFENSE_ON if running else DEFENSE_OFF)

    def _on_defense_started(self):
        self._clear_review()
        self._delete_state('defense')
        self.notebook.tab(self.defense_tab, text=TAB_DEFENSE + RUNNING)
        self._show_defense_state('● 방어 준비 중', True)

    def _on_defense_stopped(self):
        if not self.closed:
            try:
                self.notebook.tab(self.defense_tab, text=TAB_DEFENSE)
                self._show_defense_state('● 방어 꺼짐', False)
            except tk.TclError:
                pass
        if self.delete_phase == 'defense':
            self.model.invalidate()
            self._delete_state(None)
            self._render()
            self.status.set('방어 종료 · 수동 작업은 다시 조회 후 선택하세요')

    def _ensure_defense_window(self, gallery=None):
        if gallery is None:
            try:
                text = self.gallery.get().strip().lower()
                gallery = validate_gallery(GALLERIES.get(text, text))
            except ValueError:
                gallery = 'mnt'
        account = self.login_result.verified_user_id if self.login_result is not None else ''
        if self.defense_window is None or getattr(self.defense_window, 'closed', True):
            self.defense_window = DefenseWindow(
                self.defense_tab, gallery, (1,), self._defense_closed,
                task_factory=self._make_defense_factory(), account=account,
                embedded=True, before_start=self._before_defense_start,
                on_started=self._on_defense_started, on_stopped=self._on_defense_stopped)
        else:
            self.defense_window.gallery = gallery
            self.defense_window.pages = (1,)
            self.defense_window.account = account
            self.defense_window.task_factory = self._make_defense_factory()
            if hasattr(self.defense_window, 'scope_label'):
                self.defense_window.scope_label.configure(
                    text=f'갤러리: {gallery} · 최신 페이지부터 감시할 페이지 수')
        return self.defense_window

    def show_defense(self):
        if self.closed or self.busy or self.delete_phase or not self._require_login():
            return
        try:
            text = self.gallery.get().strip().lower()
            gallery = validate_gallery(GALLERIES.get(text, text))
        except ValueError:
            self.status.set('갤러리 입력 확인 필요')
            return
        self._clear_review()
        try:
            self._ensure_defense_window(gallery)
            self.notebook.select(self.defense_tab)
        except Exception:
            record_error('manager_ui.show_defense')
            self.defense_window = None
            self._delete_state(None)
            self.status.set('방어 화면을 열지 못함 · 요청 없음')

    def _defense_closed(self):
        self.defense_window = None
        if not self.closed:
            try:
                self.notebook.tab(self.defense_tab, text=TAB_DEFENSE)
                self._show_defense_state('● 방어 꺼짐', False)
            except tk.TclError:
                pass
        self.model.invalidate()
        self._delete_state(None)
        self._render()
        self.status.set('방어 창 종료 · 수동 작업은 다시 조회 후 선택하세요')
        if self.close_requested:  # DefenseWindow calls back last, after destroying itself.
            self.close()

    # --------------------------------------------------------------------- bump
    def _before_bump_start(self):
        if self.closed or self.busy or self.delete_phase is not None or not self._require_login():
            return False
        if self.bump_window is not None:
            self.bump_window.set_borrowed_session(self.login_result)
        return True

    def _on_bump_started(self):
        self._clear_review()
        self._delete_state('bump')
        self.notebook.tab(self.bump_tab, text=TAB_BUMP + RUNNING)

    def _on_bump_stopped(self):
        if not self.closed:
            try:
                self.notebook.tab(self.bump_tab, text=TAB_BUMP)
            except tk.TclError:
                pass
        if self.delete_phase == 'bump':
            self._delete_state(None)
            self.status.set('끌올 종료')

    def _ensure_bump_window(self):
        if (self.bump_window is not None and not getattr(self.bump_window, 'closed', True)
                and self._installed_bump_factory is not self.bump_window_factory):
            old, self.bump_window = self.bump_window, None
            old.on_closed = lambda: None
            old.close()
        if self.bump_window is None or getattr(self.bump_window, 'closed', True):
            self._installed_bump_factory = self.bump_window_factory
            try:
                self.bump_window = self.bump_window_factory(
                    self.bump_tab, self._bump_closed, login_result=self.login_result,
                    embedded=True, before_start=self._before_bump_start,
                    on_started=self._on_bump_started, on_stopped=self._on_bump_stopped)
            except TypeError:
                self.bump_window = self.bump_window_factory(
                    self.root, self._bump_closed, login_result=self.login_result)
        else:
            self.bump_window.set_borrowed_session(self.login_result)
        return self.bump_window

    def show_bump(self):
        if self.closed or self.busy or self.delete_phase or not self._require_login():
            return
        self._clear_review()
        try:
            self._ensure_bump_window()
            self.notebook.select(self.bump_tab)
        except Exception:
            record_error('manager_ui.show_bump')
            self.bump_window = None
            self._delete_state(None)
            self.status.set('끌올 화면을 열지 못함 · 요청 없음')

    def _bump_closed(self):
        self.bump_window = None
        if not self.closed:
            try:
                self.notebook.tab(self.bump_tab, text=TAB_BUMP)
            except tk.TclError:
                pass
        self._delete_state(None)
        self.status.set('끌올 창 종료')
        if self.close_requested:  # BumpWindow calls back last, after destroying itself.
            self.close()

    # ---------------------------------------------------------------- lifecycle
    def _poll_login(self):
        task = self.login_task
        if task is None:
            return
        try:
            request = task.captchas.get_nowait()
        except Empty:
            pass
        else:
            try:
                if not task.cancel.is_set():
                    request.answer = prompt_captcha(request.image, parent=self.root, cancelled=task.cancel.is_set)
            except Exception:
                record_error('manager_ui._poll_login')
                request.answer = None
            finally:
                request.image = b''
                request.ready.set()
        if task.state.stage != 'finished' or task.alive:
            return
        self.login_result = task.take_result()
        self.login_task = None
        self._delete_state(None)
        if self.login_result is not None:
            note = task.profile_warning or ('계정 저장됨' if task.profile_saved else '이번 실행에서만 사용')
            self.account_status.set(f'로그인됨: {self.login_result.verified_user_id} · {note}')
            if self.bump_window is not None and not getattr(self.bump_window, 'closed', True):
                self.bump_window.set_borrowed_session(self.login_result)
            if self.defense_window is not None and not getattr(self.defense_window, 'closed', True):
                self.defense_window.account = self.login_result.verified_user_id
            self._load_profiles()
            if not self.close_requested:
                self.check_permission()
        else:
            detail = task.failure_message or REASONS.get(task.state.reason, '실패')
            if self.automatic_attempt:
                self.auto_login.set(False)
                if not self._disable_auto_login():
                    detail += ' · 자동 로그인 해제 저장 실패'
            self.account_status.set('로그인 종료 · ' + detail)
        self.automatic_attempt = False

    def _poll(self):
        if self.closed:
            return
        self._poll_login()
        self._poll_delete()
        self._poll_query()
        window = self.defense_window
        if self.delete_phase == 'defense' and window is not None and not getattr(window, 'closed', True):
            # Mirror the defense tab's status line so it is readable from any tab.
            text = '● ' + window.status.get()
            if self.defense_state.get() != text:
                self.defense_state.set(text)
        self.poll_id = self.root.after(60, self._poll)
        if self.close_requested and not self.delete_phase:
            self.close()

    def close(self):
        if self.closed:
            return
        self.close_requested = True
        self._cancel_auto_login()
        for window in (self.defense_window, self.bump_window):
            if window is not None and not getattr(window, 'closed', True):
                window.close()
                if self.closed or not getattr(window, 'closed', True):
                    return
        if self.delete_phase:
            self.cancel_delete()
            if self.delete_phase:
                return
        result, self.login_result = self.login_result, None
        close_login(result)
        self.password_text.set('')
        self.closed = True
        self.delete_cancel.set()
        self.cancel.set()
        self.model.invalidate()
        self.root.after_cancel(self.poll_id)
        self.root.destroy()
