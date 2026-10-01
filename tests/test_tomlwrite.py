from __future__ import annotations

import datetime
import math
import os
import re
import stat
import sys
from importlib import resources
from pathlib import Path
from typing import Any

import pytest

from os2slice import tomlwrite
from os2slice.tomlwrite import comment_header, dumps, write_atomic

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

MODULES_EXAMPLE: dict[str, Any] = {
    "slicers": {
        "studio": {"kind": "bambu-studio-api", "url": "http://172.30.32.1:3001"},
        "orca": {
            "argv": ["flatpak", "run", "--file-forwarding", "com.orcaslicer.OrcaSlicer",
                     "@@", "{file}", "@@"],
        },
    },
    "targets": {
        "farm": {
            "kind": "bambuddy",
            "url": "http://172.30.32.1:8000",
            "folder": "Onshape",
            "manual_start": True,
            "public_url": "https://print.example.duckdns.org:8000",
            "models": {
                "X1C": {
                    "slicer": "studio",
                    "profiles": {
                        "printer": "Bambu Lab X1 Carbon 0.4 nozzle",
                        "process": "0.20mm Standard @BBL X1C",
                        "filament": "Generic PLA @BBL X1C",
                    },
                    "bed_type": "Textured PEI Plate",
                },
                "A1 Mini": {"slicer": "studio"},
            },
        },
        "voron": {"kind": "moonraker", "url": "http://voron.lan:7125"},
    },
    "printers": {
        "voron": {
            "target": "voron",
            "slicer": "orca",
            "model": "Voron 2.4 350",
            "bed_mm": [350, 350],
            "profiles": {"printer": "Voron 2.4 350 0.4 nozzle", "process": "0.20mm"},
            "materials": ["PLA black", "PETG grey"],
        },
        "X1C_01": {"slicer": "orca"},
    },
}  # fmt: skip

TRICKY: dict[str, Any] = {
    "top": "before tables",
    "quotes": 'say "hi" \\ there',
    "controls": "tab\tnl\ncr\rbell\x07del\x7fesc\x1bnul\x00bs\bff\f",
    "unicode": "Grüße, 日本語, emoji \U0001f5a8, é",
    "literal-ish": "'single' and '''triple'''",
    "multiline-ish": '"""',
    "empty_str": "",
    "empty_list": [],
    "empty_table": {},
    "nested_empty": {"a": {"b": {}}},
    "ints": [0, -1, 2**63 - 1, -(2**63)],
    "floats": [0.0, -0.0, 1.5, 1e-5, 1.5e16, 1e300, math.inf, -math.inf],
    "mixed": [1, "two", 3.0, True, [4, [5]], {"six": 6}],
    "list_of_inline": [{"a": 1}, {"b": {"c": "d"}}, {}],
    "bools": {"t": True, "f": False},
    "weird keys": {
        "with space": 1,
        "dot.ted": 2,
        "": 3,
        "ünï": 4,
        'quo"te': 5,
        "new\nline": 6,
        "bare_OK-09": 7,
    },
    "targets": {"farm": {"models": {"X1 Carbon": {"x": 1}, "a.b": {"y": 2}}}},
}


def roundtrip(data: Any, **kw: Any) -> Any:
    text = dumps(data, **kw)
    return tomllib.loads(text)


@pytest.mark.parametrize("data", [MODULES_EXAMPLE, TRICKY, {}, {"a": {}}, {"x": 1}])
def test_roundtrip(data: dict[str, Any]) -> None:
    assert roundtrip(data) == data
    assert roundtrip(data, inline_keys={"profiles"}) == data


def test_default_config_roundtrips() -> None:
    text = resources.files("os2slice").joinpath("default_config.toml").read_text("utf-8")
    loaded = tomllib.loads(text)
    out = dumps(loaded, inline_keys={"profiles"})
    assert tomllib.loads(out) == loaded
    assert dumps(tomllib.loads(out), inline_keys={"profiles"}) == out  # stable


def test_repo_example_config_roundtrips() -> None:
    example = Path(__file__).parent.parent / "os2slice.example.toml"
    if not example.exists():
        pytest.skip("no example config")
    loaded = tomllib.loads(example.read_text("utf-8"))
    assert roundtrip(loaded) == loaded


def test_nan() -> None:
    assert math.isnan(roundtrip({"n": math.nan})["n"])


def test_inline_tables_by_name() -> None:
    text = dumps(MODULES_EXAMPLE, inline_keys={"profiles"})
    assert '[targets.farm.models.X1C]\nslicer = "studio"\nprofiles = { printer = ' in text
    assert '[targets.farm.models."A1 Mini"]' in text
    assert "[targets.farm.models.X1C.profiles]" not in text
    assert "[printers.X1C_01]" in text


def test_inline_tables_by_predicate() -> None:
    def pred(p: tuple[str, ...]) -> bool:
        return p == ("printers", "voron", "profiles")

    text = dumps(MODULES_EXAMPLE, inline_keys=pred)
    assert "[targets.farm.models.X1C.profiles]" in text
    assert "profiles = { printer" in text
    assert tomllib.loads(text) == MODULES_EXAMPLE


def test_without_inline_tables_become_sections() -> None:
    text = dumps(MODULES_EXAMPLE)
    assert "[targets.farm.models.X1C.profiles]" in text


def test_scalars_before_subtables_and_insertion_order() -> None:
    data = {"t": {"sub": {"x": 1}, "a": 1, "b": 2}, "z": 0}
    assert dumps(data) == "z = 0\n\n[t]\na = 1\nb = 2\n\n[t.sub]\nx = 1\n"


def test_parent_with_only_subtables_has_no_header() -> None:
    text = dumps({"slicers": {"orca": {"name": "Orca"}}})
    assert text == '[slicers.orca]\nname = "Orca"\n'


def test_empty_table_keeps_header() -> None:
    assert dumps({"a": {}}) == "[a]\n"
    assert dumps({}) == ""


def test_escaping() -> None:
    assert dumps({"s": 'a"b\\c\nd\x01'}) == 's = "a\\"b\\\\c\\nd\\u0001"\n'
    assert dumps({"s": "日本"}) == 's = "日本"\n'


def test_quoted_keys() -> None:
    assert dumps({"a b": 1, "ok_key-1": 2}) == '"a b" = 1\nok_key-1 = 2\n'


def test_tuples_become_lists() -> None:
    assert roundtrip({"bed": (350, 350)}) == {"bed": [350, 350]}


@pytest.mark.parametrize(
    ("data", "where"),
    [
        ({"a": None}, "a"),
        ({"t": {"when": datetime.datetime(2026, 1, 1)}}, "t.when"),
        ({"t": {"l": [1, None]}}, "t.l.[1]"),
        ({"t": {"s": {1, 2}}}, "t.s"),
        ({"p": {"profiles": {"x": None}}}, "p.profiles.x"),
        ({"t": {1: "x"}}, "t"),
    ],
)
def test_unsupported_types_name_the_path(data: dict[str, Any], where: str) -> None:
    with pytest.raises(TypeError, match="^" + re.escape(where) + ": "):
        dumps(data, inline_keys={"profiles"})


def test_root_must_be_mapping() -> None:
    with pytest.raises(TypeError):
        dumps([1, 2])  # type: ignore[arg-type]


def test_out_of_range_int_and_surrogate() -> None:
    with pytest.raises(ValueError, match="64 bits"):
        dumps({"i": 2**63})
    with pytest.raises(ValueError, match="surrogate"):
        dumps({"s": "\ud800"})


def test_comment_header() -> None:
    head = comment_header("Written by os2slice.\n\nEdit with care\n[evil] = 1")
    assert head == "# Written by os2slice.\n#\n# Edit with care\n# [evil] = 1\n"
    assert tomllib.loads(head + dumps({"a": 1})) == {"a": 1}
    assert comment_header("") == "#\n"
    assert tomllib.loads(comment_header("a\rb\x0bc")) == {}


def test_write_atomic(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "config.toml"
    write_atomic(target, "a = 1\n")
    assert target.read_text() == "a = 1\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    write_atomic(target, "a = 2\n", mode=0o644)
    assert target.read_text() == "a = 2\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert sorted(os.listdir(target.parent)) == ["config.toml"]


def test_write_atomic_failure_keeps_old_file_and_no_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "config.toml"
    target.write_text("old\n")

    def boom(*a: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(tomlwrite.os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        write_atomic(target, "new\n")
    assert target.read_text() == "old\n"
    assert os.listdir(tmp_path) == ["config.toml"]
