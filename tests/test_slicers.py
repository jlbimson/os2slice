from __future__ import annotations

import os
from pathlib import Path

import pytest

from os2slice.config import SlicerConfig
from os2slice.errors import SlicerError
from os2slice.slicers import build_argv, flatpak_app_id, launch

FILE = Path("/home/u/OnshapeExports/doc/Part 1_default_20260930-090507.stl")


def s(*argv: str) -> SlicerConfig:
    return SlicerConfig(key="k", name="Test", argv=argv)


def test_placeholder_is_replaced() -> None:
    assert build_argv(s("orca-slicer", "{file}"), FILE) == ["orca-slicer", str(FILE)]


def test_file_is_appended_without_placeholder() -> None:
    assert build_argv(s("bambu-studio"), FILE) == ["bambu-studio", str(FILE)]


def test_flatpak_file_forwarding() -> None:
    argv = build_argv(
        s(
            "flatpak",
            "run",
            "--file-forwarding",
            "com.prusa3d.PrusaSlicer",
            "--single-instance",
            "@@",
            "{file}",
            "@@",
        ),
        FILE,
    )
    assert argv == [
        "flatpak",
        "run",
        "--file-forwarding",
        "com.prusa3d.PrusaSlicer",
        "--single-instance",
        "@@",
        str(FILE),
        "@@",
    ]


def test_placeholder_inside_an_argument() -> None:
    assert build_argv(s("slicer", "--load={file}"), FILE) == ["slicer", f"--load={FILE}"]


def test_hostile_looking_filename_stays_one_argument() -> None:
    evil = Path("/tmp/x; rm -rf ~ $(id).stl")
    assert build_argv(s("slicer", "{file}"), evil) == ["slicer", str(evil)]


@pytest.mark.parametrize(
    ("argv", "app"),
    [
        (
            ("flatpak", "run", "--file-forwarding", "com.orcaslicer.OrcaSlicer", "@@"),
            "com.orcaslicer.OrcaSlicer",
        ),
        (("/usr/bin/flatpak", "run", "com.x.Y"), "com.x.Y"),
        (("orca-slicer",), None),
        (("flatpak", "list"), None),
    ],
)
def test_flatpak_app_id(argv: tuple[str, ...], app: str | None) -> None:
    assert flatpak_app_id(argv) == app


def _script(tmp_path: Path, body: str) -> str:
    p = tmp_path / "fake-slicer"
    p.write_text(f"#!/bin/sh\n{body}\n")
    p.chmod(0o755)
    return str(p)


def test_launch_detached(tmp_path: Path) -> None:
    out = tmp_path / "args"
    exe = _script(tmp_path, f'echo "$@" > {out}; sleep 5')
    log = tmp_path / "slicers.log"
    launch(s(exe, "{file}"), tmp_path / "a b.stl", log)  # returns while still running
    assert out.read_text().strip() == str(tmp_path / "a b.stl")


def test_launch_quick_success(tmp_path: Path) -> None:
    launch(s(_script(tmp_path, "exit 0")), tmp_path / "a.stl", tmp_path / "l.log")


def test_launch_immediate_failure(tmp_path: Path) -> None:
    exe = _script(tmp_path, "echo boom; exit 3")
    log = tmp_path / "l.log"
    with pytest.raises(SlicerError, match="code 3"):
        launch(s(exe), tmp_path / "a.stl", log)
    assert "boom" in log.read_text()


def test_launch_missing_executable(tmp_path: Path) -> None:
    with pytest.raises(SlicerError, match="not found"):
        launch(s("definitely-not-a-slicer-xyz"), tmp_path / "a.stl", tmp_path / "l.log")
    with pytest.raises(SlicerError, match="not found"):
        launch(s(str(tmp_path / "nope")), tmp_path / "a.stl", tmp_path / "l.log")


def test_launch_non_executable(tmp_path: Path) -> None:
    p = tmp_path / "not-exec"
    p.write_text("x")
    os.chmod(p, 0o644)
    with pytest.raises(SlicerError, match="not found"):
        launch(s(str(p)), tmp_path / "a.stl", tmp_path / "l.log")
