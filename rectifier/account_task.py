"""UI login worker; password and session never travel through status messages."""
from .auth import AuthStatus, login
from .credentials import _identity, read_password
from .background_task import BackgroundTask
from .diagnostics import record_error


def _failure_message(result):
    """Display only known labels and bounded HTTP codes, never response data."""
    labels = {
        AuthStatus.REJECTED: '디시가 로그인을 수락하지 않음 · 계정·추가 인증 확인 필요',
        AuthStatus.UNSUPPORTED: '현재 로그인 응답을 처리하지 못함',
        AuthStatus.FAILED: '인증 통신 또는 응답 해석 실패',
        AuthStatus.ACCEPTED_UNVERIFIED: '로그인 수락 신호는 있으나 계정 확인에 실패함',
        AuthStatus.VERIFIED: '로그인 계정 또는 세션 확인 불일치',
    }
    known_messages = {
        '확인된 로그인 폼 구조를 찾지 못함': '로그인 화면 구조 확인 실패',
        '로그인 사전 확인이 거절됨': '디시 로그인 사전 확인에서 거절됨',
        '입력한 보안문자가 거절됨; 자동 재시도 없음': '입력한 보안문자가 거절됨',
        '보안문자가 다시 요구됨; 자동 재시도 없이 중지함': '보안문자가 다시 요구되어 중지함',
        '사용자가 보안문자 입력을 취소했거나 시간이 초과됨': '보안문자 입력 취소 또는 시간 초과',
        '보안문자 입력 시간이 초과됨': '보안문자 입력 시간 초과',
        '보안문자 입력 형식 오류': '보안문자 입력 형식 오류',
        '보안문자 입력 처리 실패; 원문은 출력하지 않음': '보안문자 입력창 처리 실패',
        'CAPTCHA 또는 추가 인증 단계는 지원하지 않음': '현재 지원하지 않는 추가 인증이 요구됨',
        '서버가 인증 요청을 제한함; 자동 재시도 없음': '디시가 인증 요청을 제한함 · 반복 로그인 중지',
        '로그인된 계정이 요청한 계정과 다름': '로그인된 계정이 입력한 아이디와 다름',
        '디시 비밀번호 변경 안내가 표시됨; 공식 로그인 화면에서 다음에 변경 또는 변경 절차 확인 필요':
            '디시 비밀번호 변경 안내 처리가 필요함',
    }
    message = known_messages.get(result.message, labels.get(result.status, '로그인 확인 실패'))
    stages = {'login_form': '로그인 화면', 'preflight': '사전 확인',
              'credentials': '계정 제출', 'sso': 'SSO 인증', 'sso_return': 'SSO 반환',
              'landing': '로그인 후 이동', 'identity': '계정 확인', 'captcha_input': '보안문자 입력'}
    if result.diagnostics:
        step = result.diagnostics[-1]
        stage = stages.get(step.stage, '인증 처리')
        code = step.http_status
        http = f' · HTTP {code}' if type(code) is int and 100 <= code <= 599 else ''
        if type(code) is int and 500 <= code <= 599:
            message = '디시 인증 서버 오류 응답'
        message += f' [{stage}{http}]'
    return message


class LoginTask(BackgroundTask):
    """Log in once and hand the verified session to the caller; no gallery scope."""
    reuse_message = '종료한 로그인 작업을 재사용할 수 없음'

    def __init__(self, account, password, *, use_saved=False,
                 login_fn=login, password_reader=read_password, on_verified=None):
        _, self.account = _identity(account.strip())
        super().__init__()
        self._login, self._password_reader = login_fn, password_reader
        self._password = password
        self.use_saved = use_saved
        self.result = None
        self.failure_message = None
        self._on_verified = on_verified
        self.profile_saved = False
        self.profile_warning = None

    def _work(self):
        result = password = None
        reason = 'worker_failed'
        try:
            password, self._password = self._password, None
            if self.cancel.is_set():
                reason = 'user_stopped'
                return
            self._publish('login')
            if self.use_saved:
                password = self._password_reader(self.account)
            if self.cancel.is_set():
                reason = 'user_stopped'
                return
            if not password:
                reason = 'credential_missing'
                return
            result = self._login(self.account, password, captcha_prompt=self._captcha)
            if self.cancel.is_set():
                reason = 'user_stopped'
            elif (not result.identity_verified or result.session is None
                  or result.verified_user_id != self.account):
                reason = 'auth_required'
                self.failure_message = _failure_message(result)
            else:
                if self._on_verified is not None:
                    try:
                        self._on_verified(self.account, password)
                        self.profile_saved = True
                    except Exception:
                        record_error('account_task._work')
                        self.profile_warning = '계정 저장 실패 · 이번 로그인은 사용 가능, 다음 실행 시 직접 로그인'
                if self.cancel.is_set():
                    reason = 'user_stopped'
                    return
                self.result, result = result, None  # Transfer ownership once.
                reason = None
        except Exception:
            record_error('account_task._work')
            reason = 'worker_failed'
        finally:
            password = self._password = self._on_verified = None
            try:
                if result is not None and result.session is not None:
                    result.session.close()
            finally:
                self._publish('finished', reason=reason)

    def take_result(self):
        if self.alive:
            raise RuntimeError('로그인 종료 대기 필요')
        result, self.result = self.result, None
        return result
