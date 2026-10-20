"""Version-independent per-user data, separate from the installed program."""
from pathlib import Path

from platformdirs import user_data_path


PROGRAM_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = user_data_path('Rectifier', appauthor=False, version=None,
                           roaming=False, ensure_exists=False)
