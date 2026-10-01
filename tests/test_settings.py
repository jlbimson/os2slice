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
    }


def test_supports_overrides_match_the_verified_shape() -> None:
    s = PrintSettings(walls=3, infill=25, supports="tree", build_plate_only=True)
    assert s.process_overrides() == {
        "wall_loops": 3,
        "sparse_infill_density": "25%",
        "enable_support": 1,
        "support_type": "tree(auto)",
        "support_on_build_plate_only": 1,
    }
    assert s.describe() == "3 walls, 25% infill, tree supports (build plate only)"


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


@pytest.mark.parametrize(
    "values",
    [
        {"walls": "3; rm -rf"},
        {"walls": "-1"},
        {"infill": "1e3"},
        {"infill": "9999"},
        {"supports": "tree(auto)"},
        {"build_plate_only": "maybe"},
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
