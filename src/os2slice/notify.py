"""Desktop notifications via notify-send. Never raises."""

from __future__ import annotations

import html
import logging
import shutil
import subprocess

log = logging.getLogger(__name__)


def notify(summary: str, body: str = "", error: bool = False) -> None:
    exe = shutil.which("notify-send")
    if exe is None:
        return
    argv = [
        exe,
        "--app-name=os2slice",
        f"--urgency={'critical' if error else 'normal'}",
        "--icon=" + ("dialog-error" if error else "document-send"),
        "--",
        summary,
        # Some notification servers render markup in the body; part names come from Onshape.
        html.escape(body, quote=False),
    ]
    try:
        subprocess.run(argv, timeout=5, check=False, capture_output=True)
    except Exception as e:  # a notification must never break the pipeline
        log.warning("notify-send failed: %s", e)
