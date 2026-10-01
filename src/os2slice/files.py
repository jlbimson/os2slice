"""Export file naming, safe writing and pruning."""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import re
import time
import unicodedata
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

EXPORT_SUFFIXES = (".stl", ".3mf", ".step")
UNSAFE_RE = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]')
SPACE_RE = re.compile(r"\s+")
MAX_NAME_BYTES = 100


def sanitize(name: str, fallback: str = "part", max_bytes: int = MAX_NAME_BYTES) -> str:
    """Make `name` safe as a single path component on Linux, Windows and macOS."""
    name = unicodedata.normalize("NFC", name)
    name = UNSAFE_RE.sub("_", name)
    name = SPACE_RE.sub(" ", name).strip(" .")
    encoded = name.encode("utf-8")[:max_bytes]
    name = encoded.decode("utf-8", errors="ignore").rstrip(" .")
    return name or fallback


def config_tag(configuration: str) -> str:
    """Short, filesystem-safe stand-in for a configuration string."""
    if not configuration:
        return "default"
    return hashlib.sha256(configuration.encode("utf-8")).hexdigest()[:8]


def export_path(
    export_dir: Path,
    document_name: str,
    part_name: str,
    configuration: str,
    fmt: str,
    now: datetime | None = None,
) -> Path:
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    folder = export_dir / sanitize(document_name, fallback="document")
    filename = f"{sanitize(part_name)}_{config_tag(configuration)}_{stamp}.{fmt}"
    return folder / filename


def write_export(path: Path, data: bytes, export_dir: Path) -> Path:
    """Write `data` without overwriting anything; returns the path actually used."""
    root = export_dir.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.parent.resolve().is_relative_to(root):
        raise ValueError(f"refusing to write outside {root}")
    stem, suffix = path.stem, path.suffix
    for n in range(1, 100):
        candidate = path if n == 1 else path.with_name(f"{stem}-{n}{suffix}")
        try:
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return candidate
    raise FileExistsError(f"too many exports named {path.name}")


def prune(export_dir: Path, keep_days: int, now: float | None = None) -> int:
    """Delete exports older than keep_days (0 = keep forever). Returns the count removed."""
    if keep_days <= 0 or not export_dir.is_dir():
        return 0
    cutoff = (now if now is not None else time.time()) - keep_days * 86400
    removed = 0
    for folder in export_dir.iterdir():
        if folder.is_symlink() or not folder.is_dir():
            continue
        for f in folder.iterdir():
            if f.is_symlink() or not f.is_file() or f.suffix.lower() not in EXPORT_SUFFIXES:
                continue
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
            except OSError as e:
                log.warning("prune: can't remove %s: %s", f, e)
        with contextlib.suppress(OSError):
            folder.rmdir()  # only succeeds if now empty
    return removed
