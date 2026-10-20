"""Windowed installed entry point. No implicit login or moderation at startup."""
from rectifier.ui import run_ui


if __name__ == '__main__':
    raise SystemExit(run_ui())
