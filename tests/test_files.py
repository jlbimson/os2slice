from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path

import pytest

from os2slice import files


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Bracket", "Bracket"),
        ("Part 1", "Part 1"),
        ("../../etc/passwd", "_.._etc_passwd"),
        ('a/b\\c:d*e?f"g<h>i|j', "a_b_c_d_e_f_g_h_i_j"),
        ("tab\there\nnew", "tab_here_new"),
        ("  lots   of   space  ", "lots of space"),
        ("...", "part"),
        ("", "part"),
        (".hidden", "hidden"),
        ("trailing dot.", "trailing dot"),
    ],
)
def test_sanitize(name: str, expected: str) -> None:
    assert files.sanitize(name) == expected


def test_sanitize_caps_utf8_bytes_on_a_char_boundary() -> None:
    out = files.sanitize("é" * 200)
    assert len(out.encode()) <= files.MAX_NAME_BYTES
    assert out == "é" * 50


def test_config_tag() -> None:
    assert files.config_tag("") == "default"
    tag = files.config_tag("List_zrSB7lcyzQWXqq=_1")
    assert len(tag) == 8 and tag.isalnum()
    assert tag != files.config_tag("List_zrSB7lcyzQWXqq=_2")


def test_export_path(tmp_path: Path) -> None:
    p = files.export_path(
        tmp_path, "My/Doc", "Part 1", "", "stl", now=datetime(2026, 9, 30, 9, 5, 7)
    )
    assert p == tmp_path / "My_Doc" / "Part 1_default_20260930-090507.stl"


def test_write_export_never_overwrites(tmp_path: Path) -> None:
    target = tmp_path / "doc" / "p.stl"
    a = files.write_export(target, b"one", tmp_path)
    b = files.write_export(target, b"two", tmp_path)
    assert (a, b) == (target, tmp_path / "doc" / "p-2.stl")
    assert a.read_bytes() == b"one" and b.read_bytes() == b"two"


def test_write_export_refuses_to_escape(tmp_path: Path) -> None:
    root = tmp_path / "exports"
    root.mkdir()
    with pytest.raises(ValueError, match="outside"):
        files.write_export(tmp_path / "elsewhere" / "p.stl", b"x", root)


def test_write_export_does_not_follow_symlinks(tmp_path: Path) -> None:
    (tmp_path / "doc").mkdir()
    victim = tmp_path / "victim"
    victim.write_text("keep")
    (tmp_path / "doc" / "p.stl").symlink_to(victim)
    out = files.write_export(tmp_path / "doc" / "p.stl", b"x", tmp_path)
    assert out.name == "p-2.stl" and victim.read_text() == "keep"


def test_prune(tmp_path: Path) -> None:
    now = time.time()
    doc = tmp_path / "doc"
    doc.mkdir()
    old, new, other = doc / "old.stl", doc / "new.stl", doc / "notes.txt"
    for f in (old, new, other):
        f.write_bytes(b"x")
    os.utime(old, (now - 40 * 86400, now - 40 * 86400))
    os.utime(other, (now - 40 * 86400, now - 40 * 86400))
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "gone.stl").write_bytes(b"x")
    os.utime(empty / "gone.stl", (now - 40 * 86400, now - 40 * 86400))

    assert files.prune(tmp_path, keep_days=30, now=now) == 2
    assert not old.exists() and new.exists() and other.exists()
    assert not empty.exists()


def test_prune_disabled_or_missing(tmp_path: Path) -> None:
    assert files.prune(tmp_path, keep_days=0) == 0
    assert files.prune(tmp_path / "nope", keep_days=30) == 0
