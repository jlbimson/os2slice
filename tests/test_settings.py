from __future__ import annotations

import pytest

from os2slice.errors import BadRequest
from os2slice.settings import PrintSettings

DEFAULTS = PrintSettings()


def test_defaults_and_overrides() -> None:
    assert DEFAULTS.process_overrides() == {
        "wall_loops": 2,
        "sparse_infill_density": "15%",
        "enable_support": 0,
        "top_shell_layers": 5,
        "bottom_shell_layers": 3,
        "brim_type": "no_brim",
    }


def test_supports_overrides_match_the_verified_shape() -> None:
    s = PrintSettings(walls=3, infill=25, supports="tree", build_plate_only=True)
    assert s.process_overrides() == {
        "wall_loops": 3,
        "sparse_infill_density": "25%",
        "enable_support": 1,
        "support_type": "tree(auto)",
        "support_on_build_plate_only": 1,
        "top_shell_layers": 5,
        "bottom_shell_layers": 3,
        "brim_type": "no_brim",
    }
    assert s.describe() == (
        "3 walls, 25% infill, tree supports (build plate only), 5 top / 3 bottom layers, no brim"
    )


def test_brim_shells_and_copies() -> None:
    s = PrintSettings(top_layers=7, bottom_layers=0, brim=True, copies=4)
    o = s.process_overrides()
    assert (o["top_shell_layers"], o["bottom_shell_layers"], o["brim_type"]) == (7, 0, "outer_only")
    assert "copies" not in str(o)  # laid out by os2slice, not a slicer key
    assert s.describe().endswith("7 top / 0 bottom layers, brim, 4 copies")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"walls": 0},
        {"walls": 11},
        {"walls": True},
        {"infill": -1},
        {"infill": 101},
        {"infill": 12.5},
        {"supports": "everywhere"},
        {"build_plate_only": "yes"},
        {"top_layers": -1},
        {"top_layers": 31},
        {"bottom_layers": 2.0},
        {"brim": 1},
        {"copies": 0},
        {"copies": 26},
    ],
)
def test_out_of_range(kwargs: dict) -> None:
    with pytest.raises(BadRequest):
        PrintSettings(**kwargs)


def test_from_strings() -> None:
    s = PrintSettings.from_strings(
        {"walls": "4", "infill": "40%", "supports": "normal", "build_plate_only": "on"}, DEFAULTS
    )
    assert s == PrintSettings(4, 40, "normal", True)
    assert PrintSettings.from_strings({}, DEFAULTS) == DEFAULTS
    assert PrintSettings.from_strings({"walls": ""}, DEFAULTS).walls == 2
    s = PrintSettings.from_strings(
        {"top_layers": "6", "bottom_layers": "4", "brim": "true", "copies": "3"}, DEFAULTS
    )
    assert (s.top_layers, s.bottom_layers, s.brim, s.copies) == (6, 4, True, 3)
    assert not PrintSettings.from_strings({"brim": ""}, PrintSettings(brim=True)).brim


@pytest.mark.parametrize(
    "values",
    [
        {"walls": "3; rm -rf"},
        {"walls": "-1"},
        {"infill": "1e3"},
        {"infill": "9999"},
        {"supports": "tree(auto)"},
        {"build_plate_only": "maybe"},
        {"brim": "outer_only"},
        {"top_layers": "5.5"},
        {"copies": "-2"},
        {"copies": "100"},
    ],
)
def test_from_strings_rejects_junk(values: dict) -> None:
    with pytest.raises(BadRequest):
        PrintSettings.from_strings(values, DEFAULTS)


def test_smooth_pei_is_high_temp_plate() -> None:
    from os2slice.settings import PLATE_LABELS, check_bed_type

    assert check_bed_type("Smooth PEI Plate") == "High Temp Plate"
    assert check_bed_type("High Temp Plate") == "High Temp Plate"
    assert "Smooth PEI Plate" not in PLATE_LABELS  # not offered twice
    assert check_bed_type("") is None
