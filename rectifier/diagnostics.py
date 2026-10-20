"""Best-effort record of exceptions the program deliberately swallows.

Only the place and the exception type name are written. Messages, arguments and
tracebacks can carry server text, account names or secrets, so they never leave memory.
"""
from datetime import datetime, timezone
import sys

from . import __version__
from .paths import DATA_ROOT
from .performance import write_summary

ERROR_PATH = DATA_ROOT / 'diagnostics' / 'errors.jsonl'
ENABLED = True  # The offline test suite turns this off so it never writes to the real profile.


def record_error(where, error=None):
    """Call inside an ``except`` block. Never raises; returns whether a line was written."""
    if not ENABLED:
        return False
    try:
        if error is None:
            error = sys.exc_info()[1]
        name = type(error).__name__ if error is not None else 'unknown'
        return write_summary({'kind': 'swallowed_exception', 'version': __version__,
                              'at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                              'where': str(where)[:80], 'type': name[:80]}, path=ERROR_PATH)
    except Exception:
        return False
