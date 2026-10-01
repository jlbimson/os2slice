from __future__ import annotations

import pytest

from os2slice.filaments import Slot, color_name, match_preset, preset_suffix, slots_from_status
from tests.fakes import FILAMENT_PRESETS, STATUS_FILAMENT

NAMES = [
    *FILAMENT_PRESETS,
    "Generic PETG HF @BBL X1C",
    "Bambu PETG-CF @BBL X1C",
    "Bambu Support For PLA/PETG @BBL X1C",
    "Bambu Support for ABS @BBL H2D",
    "Bambu TPU 95A @BBL X1C",
    "Generic PLA @BBL A1M 0.2 nozzle",
]


def test_slots_from_live_shaped_status() -> None:
    slots = slots_from_status(STATUS_FILAMENT[2])
    assert [(s.tray_id, s.label, s.material, s.color) for s in slots] == [
        (0, "AMS 1 · slot 1", "ASA", "#FFF144"),
        (1, "AMS 1 · slot 2", "PLA", "#FFFFFF"),
        (2, "AMS 1 · slot 3", "PEEK", "#161616"),
        (254, "External", "PETG", "#161616"),
    ]  # the empty slot 4 is skipped
    assert slots[-1].external and not slots[0].external
    assert slots[0].describe() == "AMS 1 · slot 1: ASA · yellow"


def test_second_ams_ht_and_two_externals() -> None:
    status = {
        "ams": [
            {"id": 1, "tray": [{"id": 2, "tray_type": "PLA", "tray_color": "FF0000FF"}]},
            {"id": 128, "tray": [{"id": 0, "tray_type": "PA-CF", "tray_color": "000000FF"}]},
        ],
        "vt_tray": [
            {"id": 254, "tray_type": "PET-CF", "tray_color": "000000FF"},
            {"id": 255, "tray_type": "TPU", "tray_color": "898989FF"},
        ],
    }
    assert [(s.tray_id, s.label) for s in slots_from_status(status)] == [
        (6, "AMS 2 · slot 3"),
        (128, "AMS HT 1"),
        (254, "External 1"),
        (255, "External 2"),
    ]


@pytest.mark.parametrize(
    ("material", "brand", "suffix", "expected"),
    [
        ("PETG", "", "@BBL A1M", "Generic PETG @BBL A1M"),
        ("PETG", "", "@BBL X1C", "Bambu PETG Basic @BBL X1C"),
        ("ASA", "", "@BBL X1C", "Bambu ASA @BBL X1C"),
        ("PLA", "", "@BBL H2D", "Generic PLA @BBL H2D"),
        ("TPU", "", "@BBL X1C", "Bambu TPU 95A @BBL X1C"),
        ("ABS-S", "Support for ABS", "@BBL H2D", "Bambu Support for ABS @BBL H2D"),
        ("PEEK", "", "@BBL X1C", None),
        ("PLA", "", "", None),
    ],
)
def test_match_preset(material: str, brand: str, suffix: str, expected: str | None) -> None:
    slot = Slot(0, "AMS 1 · slot 1", material, "#FFFFFF", brand)
    assert match_preset(slot, suffix, NAMES) == expected


def test_nozzle_variants_and_cf_are_not_confused() -> None:
    slot = Slot(0, "x", "PLA", "#FFFFFF")
    assert match_preset(slot, "@BBL A1M", NAMES) == "Bambu PLA Basic @BBL A1M"
    petg = Slot(0, "x", "PETG", "#000000")
    assert match_preset(petg, "@BBL X1C", ["Bambu PETG-CF @BBL X1C"]) is None


def test_helpers() -> None:
    assert preset_suffix("Bambu PLA Basic @BBL A1M") == "@BBL A1M"
    assert preset_suffix("My PLA") == ""
    assert [color_name(c) for c in ("#FFF144", "#161616", "#FFFFFF", "#898989", "nope")] == [
        "yellow", "black", "white", "grey", "unknown colour",
    ]  # fmt: skip


def test_dual_nozzle_slots_know_their_nozzle() -> None:
    status = {
        "ams_extruder_map": {"0": 1, "1": 0},
        "ams": [
            {"id": 0, "tray": [{"id": 0, "tray_type": "PETG", "tray_color": "161616FF"}]},
            {"id": 1, "tray": [{"id": 2, "tray_type": "PLA", "tray_color": "FFFFFFFF"}]},
        ],
        "vt_tray": [
            {"id": 254, "tray_type": "PET-CF", "tray_color": "000000FF"},
            {"id": 255, "tray_type": "TPU", "tray_color": "898989FF"},
        ],
    }
    slots = slots_from_status(status, dual_nozzle=True)
    assert [(s.tray_id, s.label, s.extruder) for s in slots] == [
        (0, "AMS 1 · slot 1", 1),
        (6, "AMS 2 · slot 3", 0),
        (254, "External left", 1),
        (255, "External right", 0),
    ]
    assert slots[0].describe() == "AMS 1 · slot 1: PETG · black (left nozzle)"
    assert all(s.extruder is None for s in slots_from_status(status))  # single-nozzle view


def test_sliced_nozzle_reads_bambuddy_metadata() -> None:
    from os2slice.filaments import sliced_nozzle
    from tests.fakes import dual_nozzle_3mf

    assert sliced_nozzle(dual_nozzle_3mf()) == 1  # group 0 → extruder index 0 → physical 1
    assert sliced_nozzle(dual_nozzle_3mf(extruder_id=2)) == 0
    assert sliced_nozzle(dual_nozzle_3mf(physical=("0",))) is None  # single nozzle
    assert sliced_nozzle(b"not a zip") is None
