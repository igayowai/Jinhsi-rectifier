"""One explicitly started UI task. Tk work remains on the main thread."""
from .auth import login
from .background_task import BackgroundTask, CaptchaRequest, TaskState  # re-exported
from .credentials import _identity, read_password
from .defense_runner import DefenseReadError, DefenseRunner, ListDefenseAdapter
from .models import validate_gallery
from .policy import validate_manual_pages
from .request_budget import RequestBudget
from .diagnostics import record_error


class DefenseTask(BackgroundTask):
    reuse_message = '종료한 방어 작업을 재사용할 수 없음'

    def __init__(self, gallery, pages, *, account=None, login_result=None,
                 password_reader=read_password, login_fn=login, include_new_members=False):
        self.gallery = validate_gallery(gallery)
        self.pages = validate_manual_pages(pages)
        if self.pages != tuple(range(1, len(self.pages) + 1)):
            raise ValueError('방어모드는 최신 1페이지부터 연속 조회해야 함')
        if type(include_new_members) is not bool:
            raise ValueError('신규 회원 포함 설정 오류')
        self.include_new_members = include_new_members
        self._password_reader, self._login = password_reader, login_fn
        self._borrowed = login_result
        selected = login_result.verified_user_id if login_result is not None else account
        _, self.account = _identity(selected)
        super().__init__()

    def _work(self):
        result = None
        password = None
        stats = None
        reason = 'worker_failed'
        try:
            if self.cancel.is_set():
                reason = 'user_stopped'
                return
            self._publish('login')
            result = self._borrowed
            if result is None:
                password = self._password_reader(self.account)
                if not password:
                    reason = 'credential_missing'
                    return
                if self.cancel.is_set():
                    reason = 'user_stopped'
                    return
                try:
                    result = self._login(self.account, password, captcha_prompt=self._captcha)
                finally:
                    password = None
            if self.cancel.is_set():
                reason = 'user_stopped'
                return
            if not result.identity_verified or result.verified_user_id != self.account:
                reason = 'auth_required'
                return
            adapter = ListDefenseAdapter(result, self.gallery, pages=self.pages,
                                         request_budget=RequestBudget(30),
                                         include_recommended=True,
                                         include_new_members=self.include_new_members)
            self._publish('baseline')
            boundary = adapter.read_start_boundary(self.cancel)
            runner = DefenseRunner(adapter, after_post_id=boundary, cancel=self.cancel, delete_workers=12,
                                   posts_per_request=5)
            self._publish('running', runner.stats)
            stats = runner.run(lambda value: self._publish('running', value))
            reason = stats.stopped
        except DefenseReadError as error:
            reason = str(error)
        except Exception:
            record_error('defense_task._work')
            stats = self.state.stats
            reason = 'worker_failed'
        finally:
            password = None
            try:
                if self._borrowed is None and result is not None and result.session is not None:
                    result.session.close()
            except Exception:
                record_error('defense_task._work')
                reason = 'worker_failed'
            finally:
                self._publish('finished', stats, reason)
