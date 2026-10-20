"""Account IDs/preferences only. Secrets stay in the existing Windows vault."""
from contextlib import closing
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3

from .credentials import _identity, save_verified_password
from .models import validate_gallery
from .paths import DATA_ROOT
from .diagnostics import record_error


MAX_RECENT_GALLERIES = 20


class ProfileError(RuntimeError):
    pass


@dataclass(frozen=True)
class Profiles:
    accounts: tuple[str, ...] = ()
    last_account: str = ''
    auto_login: bool = False


class AccountProfiles:
    def __init__(self, path=None, *, password_writer=save_verified_password):
        self.path = Path(path) if path is not None else DATA_ROOT / 'accounts.sqlite'
        self._password_writer = password_writer

    def load(self):
        if not self.path.exists():
            return Profiles()
        try:
            with closing(sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
                accounts = tuple(row[0] for row in db.execute('SELECT account FROM accounts ORDER BY account'))
                row = db.execute('SELECT last_account, auto_login FROM settings WHERE id=1').fetchone()
            if any(_identity(account)[1] != account for account in accounts):
                raise ValueError('invalid account')
            if row is None or row[1] not in (0, 1) or (row[0] and row[0] not in accounts):
                raise ValueError('invalid settings')
            return Profiles(accounts, row[0], bool(row[1]))
        except Exception:
            record_error('account_profiles.load')
            raise ProfileError('저장 계정 목록을 읽지 못함 · 직접 로그인 가능') from None

    def remember(self, account, password, *, auto_login, write_password=True):
        _, account = _identity(account)
        self.load()  # Do not replace a damaged registry or change its credentials.
        try:
            if write_password:
                self._password_writer(account, password)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path, timeout=3)) as db, db:
                db.execute('CREATE TABLE IF NOT EXISTS accounts (account TEXT PRIMARY KEY)')
                db.execute('CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), '
                           'last_account TEXT NOT NULL, auto_login INTEGER NOT NULL CHECK(auto_login IN (0,1)))')
                db.execute('INSERT OR IGNORE INTO accounts VALUES (?)', (account,))
                db.execute('INSERT INTO settings VALUES (1, ?, ?) ON CONFLICT(id) DO UPDATE SET '
                           'last_account=excluded.last_account, auto_login=excluded.auto_login',
                           (account, int(bool(auto_login))))
        except Exception:
            record_error('account_profiles.remember')
            raise ProfileError('계정 저장 실패 · 이번 로그인은 사용 가능, 다음 실행 시 직접 로그인') from None

    def disable_auto(self, account=None):
        if not self.path.exists():
            return
        try:
            with closing(sqlite3.connect(self.path.resolve().as_uri() + '?mode=rw', uri=True,
                                        timeout=3)) as db, db:
                if account is None:
                    db.execute('UPDATE settings SET auto_login=0 WHERE id=1')
                else:
                    db.execute('UPDATE settings SET auto_login=0 WHERE id=1 AND last_account=?', (account,))
        except Exception:
            record_error('account_profiles.disable_auto')
            raise ProfileError('자동 로그인 해제 저장 실패 · 다음 실행 시 자동 로그인할 수 있음') from None


class GalleryStore:
    """Persist validated gallery IDs in a separate JSON file outside accounts.sqlite."""
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else DATA_ROOT / 'recent-galleries.json'

    def load(self) -> tuple[str, ...]:
        if not self.path.exists():
            return ()
        try:
            raw = json.loads(self.path.read_text(encoding='utf-8'))
            items = raw.get('galleries') if isinstance(raw, dict) else raw
            if not isinstance(items, list):
                raise ValueError('invalid gallery list')
            result = []
            for item in items:
                gallery = validate_gallery(item)
                if gallery not in result:
                    result.append(gallery)
                if len(result) >= MAX_RECENT_GALLERIES:
                    break
            return tuple(result)
        except Exception:
            record_error('account_profiles.gallery_load')
            return ()

    def remember(self, gallery_id: str) -> tuple[str, ...]:
        try:
            gallery = validate_gallery(gallery_id)
        except ValueError:
            return ()
        existing = self.load()
        updated = (gallery, *(item for item in existing if item != gallery))[:MAX_RECENT_GALLERIES]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({'galleries': list(updated)}, ensure_ascii=False, indent=2),
                                 encoding='utf-8')
        except Exception:
            record_error('account_profiles.gallery_remember')
        return updated


# Small helpers shared by the main window and the bump window, so both treat
# saved accounts and sessions the same way.
def remember_after_login(store, *, auto_login, write_password):
    """LoginTask ``on_verified`` callback: save only after the server accepted the login."""
    return lambda account, secret: store.remember(account, secret, auto_login=auto_login,
                                                  write_password=write_password)


def disable_auto_login(store):
    """True when there is no store or auto-login was turned off; False when the store refused."""
    if store is None:
        return True
    try:
        store.disable_auto()
        return True
    except Exception:
        record_error('account_profiles.disable_auto_login')
        return False


def close_login(result):
    """Close a login session; a close failure never escapes into the UI."""
    if result is None or getattr(result, 'session', None) is None:
        return
    try:
        result.session.close()
    except Exception:
        record_error('account_profiles.close_login')
