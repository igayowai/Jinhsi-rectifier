"""Absolute-path CLI entry point; also the future console worker build entry."""
from rectifier.__main__ import main
import sys


if __name__ == '__main__':
    # Frozen console workers do not inherit Python's -X utf8 option.
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            stream.reconfigure(encoding='utf-8', errors='replace')
    raise SystemExit(main())
