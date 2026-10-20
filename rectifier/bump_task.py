"""Explicit bump task using the existing login and captcha mechanisms."""
import re

from .auth import login
from .credentials import read_password
from .background_task import BackgroundTask
from .bump import BumpAdapter, BumpRunner, BumpStopped, MultiBumpAdapter, validate_bump_targets
from .diagnostics import record_error


class BumpTask(BackgroundTask):
    reuse_message = '종료한 끌올 작업을 재사용할 수 없음'

    def __init__(self, target, account, limits, *, password=None,
                 password_reader=read_password, login_fn=login, second_target=None, login_result=None,
                 additional_targets=()):
        if not isinstance(account, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', account):
            raise ValueError('로그인 ID를 입력해야 함')
        additional_targets = tuple(additional_targets)
        if additional_targets and second_target is None:
            raise ValueError('두 번째 글 주소를 입력해야 함')
        targets = (target,) if second_target is None else (target, second_target, *additional_targets)
        if len(targets) > 1:
            validate_bump_targets(targets)
        self.targets = targets
        self.target, self.account, self.limits = target, account.lower(), limits
        self.second_target = second_target
        self.pair_progress = None
        self._password, self._password_reader, self._login = password, password_reader, login_fn
        # The window owns a borrowed session and closes it only after this worker ends.
        self._login_result = login_result
        super().__init__()
        self.title = ''

    def stop(self):
        super().stop()
        self._password = None

    def _work(self):
        result = password = runner = None
        stats, reason = None, 'worker_failed'
        try:
            if self.cancel.is_set():
                reason = 'user_stopped'
                return
            result = self._login_result
            if result is None:
                self._publish('login')
                password, self._password = self._password, None
                if not password:
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
            if self.second_target is None:
                adapter = BumpAdapter(result, self.target, self.account)
            else:
                adapter = MultiBumpAdapter(result, self.targets, self.account)
            runner = BumpRunner(adapter, self.limits, cancel=self.cancel)
            self._publish('checking')

            def progress(value):
                if self.second_target is None:
                    self.title = adapter.title
                else:
                    self.pair_progress = adapter.progress
                self._publish('running', value)

            stats = runner.run(progress)
            reason = stats.stopped
        except BumpStopped as exc:
            reason = str(exc)
        except Exception:
            record_error('bump_task._work')
            stats = runner.stats if runner is not None else self.state.stats
            reason = 'worker_failed'
        finally:
            self._password = password = None
            try:
                if result is not None and result is not self._login_result and result.session is not None:
                    result.session.close()
            except Exception:
                record_error('bump_task._work')
                reason = 'worker_failed'
            finally:
                self._login_result = None
                self._publish('finished', stats, reason)
