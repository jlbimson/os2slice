"""Rotating file log under ~/.local/state/os2slice/."""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "os2slice"


def log_path() -> Path:
    return state_dir() / "os2slice.log"


def setup_logging(verbose: bool = False, console: bool = True) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for h in list(root.handlers):
        root.removeHandler(h)

    try:
        state_dir().mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(log_path(), maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(logging.Formatter(LOG_FORMAT))
        fh.setLevel(logging.DEBUG)
        root.addHandler(fh)
    except OSError as e:  # never let logging setup stop the tool
        print(f"os2slice: can't open log file: {e}", file=sys.stderr)

    if console:
        ch = logging.StreamHandler(sys.stderr)
        ch.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        ch.setLevel(logging.DEBUG if verbose else logging.WARNING)
        root.addHandler(ch)

    # httpx logs full request URLs at INFO; keep them in the file only at DEBUG.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
