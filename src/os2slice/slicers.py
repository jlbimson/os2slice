"""Build slicer command lines from config and launch them detached. Never uses a shell."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path

from os2slice.config import SlicerConfig
from os2slice.errors import SlicerError

log = logging.getLogger(__name__)

FILE_PLACEHOLDER = "{file}"
MAX_SLICER_LOG = 5_000_000
EARLY_EXIT_WAIT = 1.5  # seconds to watch for "command failed immediately"


def build_argv(slicer: SlicerConfig, file: Path) -> list[str]:
    """Substitute {file} into the configured argv; append the file if no placeholder."""
    path = str(file)
    if any(FILE_PLACEHOLDER in a for a in slicer.argv):
        return [a.replace(FILE_PLACEHOLDER, path) for a in slicer.argv]
    return [*slicer.argv, path]


def resolve_executable(argv0: str) -> str | None:
    if os.sep in argv0:
        p = Path(argv0).expanduser()
        return str(p) if p.is_file() and os.access(p, os.X_OK) else None
    return shutil.which(argv0)


def flatpak_app_id(argv: tuple[str, ...] | list[str]) -> str | None:
    """The app ID in `flatpak run [opts] <app-id> ...`, if that's what argv is."""
    if not argv or Path(argv[0]).name != "flatpak" or "run" not in argv:
        return None
    for a in argv[argv.index("run") + 1 :]:
        if not a.startswith("-"):
            return a
    return None


def launch(slicer: SlicerConfig, file: Path, log_file: Path) -> None:
    """Start the slicer in its own session and return without waiting for it."""
    argv = build_argv(slicer, file)
    exe = resolve_executable(argv[0])
    if exe is None:
        raise SlicerError(
            f"{slicer.name} not found: {argv[0]}",
            f"Fix slicers.{slicer.key}.argv in config.toml",
        )
    argv[0] = exe

    log_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        if log_file.stat().st_size > MAX_SLICER_LOG:
            log_file.unlink()
    except FileNotFoundError:
        pass

    log.info("launching %s: %s", slicer.name, argv)
    with log_file.open("ab") as out:
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
                cwd=Path.home(),
            )
        except OSError as e:
            raise SlicerError(
                f"Couldn't start {slicer.name}: {e.strerror or e}",
                f"Fix slicers.{slicer.key}.argv in config.toml",
            ) from e

    try:
        code = proc.wait(timeout=EARLY_EXIT_WAIT)
    except subprocess.TimeoutExpired:
        # Still running: normal. Reap it in the background so no zombie is left behind.
        threading.Thread(target=proc.wait, daemon=True).start()
        return
    if code != 0:
        raise SlicerError(f"{slicer.name} exited right away with code {code}", f"See {log_file}")
