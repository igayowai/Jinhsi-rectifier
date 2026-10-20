"""Thin Windows Credential Manager adapter for Rectifier-owned entries only."""
import re
import sys

from .diagnostics import record_error


class CredentialError(RuntimeError):
    """A fixed user message, without backend exception details or secret values."""


def _identity(account: str) -> tuple[str, str]:
    if not isinstance(account, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", account):
        raise CredentialError("로그인 ID 형식이 올바르지 않음")
    # Windows target names are case-insensitive. Normalize both parts together.
    normalized = account.lower()
    return "rectifier:dcinside:" + normalized, normalized


def _backend():
    if sys.platform != "win32":
        raise CredentialError("자동 입력 저장소는 Windows에서만 지원함. --manual 사용 가능")
    try:
        from keyring.backends.Windows import WinVaultKeyring
        backend = WinVaultKeyring()
        backend.persist = "local machine"
        # Never use keyring backend autodiscovery or a plaintext fallback.
        return backend
    except Exception:
        record_error('credentials._backend')
        raise CredentialError("Windows 자격 증명 저장소 준비 실패. requirements.txt 설치 확인 필요") from None


def read_password(account: str) -> str | None:
    service, username = _identity(account)
    try:
        password = _backend().get_password(service, username)
        if password is not None and (not isinstance(password, str) or not password):
            raise CredentialError("저장된 비밀번호 형식 오류. 해당 정류기 항목 재등록 필요")
        return password
    except CredentialError:
        raise
    except Exception:
        record_error('credentials.read_password')
        raise CredentialError("Windows 자격 증명 조회 실패") from None


def save_password_new(account: str, password: str):
    _save_password(account, password, replace=False)


def save_verified_password(account: str, password: str):
    """Caller must first verify login as this exact account."""
    _save_password(account, password, replace=True)


def _save_password(account: str, password: str, *, replace: bool):
    service, username = _identity(account)
    if (not isinstance(password, str) or not password
            or any(char in password for char in "\r\n\0")
            or len(password.encode("utf-16-le")) > 2560):
        raise CredentialError("저장할 비밀번호 형식이 올바르지 않음")
    try:
        backend = _backend()
        if not replace and backend.get_password(service, username) is not None:
            raise CredentialError("기존 정류기 비밀번호가 있음. 덮어쓰지 않았음")
        backend.set_password(service, username, password)
        if backend.get_password(service, username) != password:
            raise CredentialError("저장 결과 확인 실패. 정류기 자격 증명 항목 확인 필요")
    except CredentialError:
        raise
    except Exception:
        record_error('credentials._save_password')
        raise CredentialError("Windows 자격 증명 저장 실패") from None


def forget_password(account: str) -> bool:
    service, username = _identity(account)
    try:
        backend = _backend()
        if backend.get_password(service, username) is None:
            return False
        backend.delete_password(service, username)
        if backend.get_password(service, username) is not None:
            raise CredentialError("저장값 제거 결과 확인 실패")
        return True
    except CredentialError:
        raise
    except Exception:
        record_error('credentials.forget_password')
        raise CredentialError("정류기 자격 증명 제거 실패") from None
