"""OrcaSlicer GUI profiles on disk (modules/orca_profiles.py), against a small fake config
folder laid out like ~/.config/OrcaSlicer (values from a real one)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from os2slice.modules.base import ModuleError
from os2slice.modules.orca_profiles import OrcaProfiles

USER = "0d1e5a7c-0000-4000-8000-000000000001"


def write(path: Path, body: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body))


def vendor(
    root: Path, name: str, presets: dict[str, list[tuple[str, str, dict[str, Any]]]]
) -> None:
    """system/<name>.json and its preset files: kind -> [(preset name, sub_path, body)]."""
    meta: dict[str, Any] = {"name": name}
    for kind, entries in presets.items():
        meta[f"{kind}_list"] = [{"name": n, "sub_path": sub} for n, sub, _ in entries]
        for n, sub, body in entries:
            write(root / "system" / name / sub, {"name": n, **body})
    write(root / "system" / f"{name}.json", meta)


@pytest.fixture
def orca(tmp_path: Path) -> Path:
    root = tmp_path / "OrcaSlicer"
    vendor(
        root,
        "Ratrig",
        {
            "machine": [
                (
                    "fdm_klipper_common",
                    "machine/fdm_klipper_common.json",
                    {"gcode_flavor": "klipper"},
                ),
                (
                    "RatRig V-Core 3 300 0.4 nozzle",
                    "machine/RatRig V-Core 3 300 0.4 nozzle.json",
                    {"inherits": "fdm_klipper_common", "nozzle_diameter": ["0.4"]},
                ),
            ],
            "filament": [
                (
                    "fdm_filament_asa",
                    "filament/fdm_filament_asa.json",
                    {"nozzle_temperature": ["250"]},
                ),
                (
                    "RatRig Generic ASA",
                    "filament/RatRig Generic ASA.json",
                    {"inherits": "fdm_filament_asa", "filament_density": ["1.1"]},
                ),
            ],
        },
    )
    vendor(
        root,
        "OrcaFilamentLibrary",
        {
            "filament": [
                (
                    "fdm_filament_common",
                    "filament/base/fdm_filament_common.json",
                    {
                        "chamber_temperatures": ["0"],
                        "filament_density": ["1.24"],
                        "filament_is_support": ["0"],
                        "setting_id": "GFSA04",
                    },
                ),
                (
                    "fdm_filament_asa",  # same name as RatRig's, different values
                    "filament/base/fdm_filament_asa.json",
                    {"inherits": "fdm_filament_common", "nozzle_temperature": ["260"]},
                ),
                (
                    "Generic ASA @System",
                    "filament/Generic ASA @System.json",
                    {"inherits": "fdm_filament_asa", "instantiation": "true"},
                ),
            ]
        },
    )
    user = root / "user" / USER
    write(
        user / "machine" / "JoshPrint 0.5 MMU.json",
        {"name": "JoshPrint 0.5 MMU", "inherits": "RatRig V-Core 3 300 0.4 nozzle", "from": "User",
         "nozzle_diameter": ["0.5"]},
    )  # fmt: skip
    write(user / "process" / "0.2 Strong.json", {"name": "0.2 Strong", "from": "User"})
    write(
        user / "filament" / "PM ASA.json",
        {"name": "PM ASA", "inherits": "RatRig Generic ASA", "from": "User"},
    )
    bundle = user / "_local" / "27ff2b16-4b8f-4bd4-8865-b1df168bbf76" / "filament"
    write(
        bundle / "Sirayatech PET-CF.json",
        {"name": "Sirayatech PET-CF", "inherits": "Generic ASA @System", "from": "Bundle",
         "chamber_temperature": ["50"], "nozzle_temperature": ["300"]},
    )  # fmt: skip
    write(bundle / "PM ASA.json", {"name": "PM ASA", "inherits": "Generic ASA @System"})
    write(
        root / "user" / "default" / "filament" / "Signed out PLA.json", {"name": "Signed out PLA"}
    )
    return root


def test_names_from_every_user_folder_and_bundle(orca: Path) -> None:
    profiles = OrcaProfiles.load(orca)
    assert profiles.names("machine") == ("JoshPrint 0.5 MMU",)
    assert profiles.names("process") == ("0.2 Strong",)
    assert profiles.names("filament") == ("PM ASA", "Signed out PLA", "Sirayatech PET-CF")
    # Your own PM ASA wins over the bundle's copy of the same name.
    assert profiles.users["filament"]["PM ASA"]["inherits"] == "RatRig Generic ASA"


def test_a_single_user_folder_finds_the_system_folder_two_levels_up(orca: Path) -> None:
    profiles = OrcaProfiles.load(orca / "user" / USER)
    assert "Signed out PLA" not in profiles.names("filament")
    flat = profiles.flatten(profiles.users["filament"]["PM ASA"], "filament")
    assert flat["nozzle_temperature"] == ["250"]


def test_flatten_walks_the_parents_within_their_own_vendor(orca: Path) -> None:
    profiles = OrcaProfiles.load(orca)
    ratrig = profiles.flatten(profiles.users["filament"]["PM ASA"], "filament")
    assert ratrig["nozzle_temperature"] == ["250"]  # RatRig's fdm_filament_asa
    assert ratrig["filament_density"] == ["1.1"] and "inherits" not in ratrig
    library = profiles.flatten(profiles.users["filament"]["Sirayatech PET-CF"], "filament")
    assert library["nozzle_temperature"] == ["300"]  # its own value
    assert library["filament_density"] == ["1.24"]  # the library's base, not RatRig's
    assert library["instantiation"] == "true" and library["name"] == "Sirayatech PET-CF"
    assert library["from"] == "system" and library["type"] == "filament"  # never "Bundle"


def test_legacy_keys_are_renamed_before_merging(orca: Path) -> None:
    profiles = OrcaProfiles.load(orca)
    flat = profiles.flatten(profiles.users["filament"]["Sirayatech PET-CF"], "filament")
    # The base's old `chamber_temperatures` would otherwise beat the user's value in the CLI.
    assert flat["chamber_temperature"] == ["50"] and "chamber_temperatures" not in flat


def test_machine_flattened_through_two_levels(orca: Path) -> None:
    profiles = OrcaProfiles.load(orca)
    flat = profiles.flatten(profiles.users["machine"]["JoshPrint 0.5 MMU"], "machine")
    assert flat["nozzle_diameter"] == ["0.5"] and flat["gcode_flavor"] == "klipper"


def test_unknown_or_bbl_parents_are_left_for_the_sidecar(orca: Path) -> None:
    vendor(orca, "BBL", {"filament": [("Bambu PLA Basic @BBL A1M", "filament/x.json", {})]})
    profiles = OrcaProfiles.load(orca)
    for parent in ("Bambu PLA Basic @BBL A1M", "Nobody's preset"):
        body = {"name": "Mine", "inherits": parent, "from": "User"}
        assert profiles.flatten(body, "filament") == body


def test_index_paths_outside_system_are_ignored(orca: Path) -> None:
    write(
        orca / "system" / "Evil.json",
        {"filament_list": [{"name": "Escape", "sub_path": "../../user/x.json"}]},
    )
    assert ("Evil", "filament") not in OrcaProfiles.load(orca).system


def test_inheritance_loop_refused(orca: Path) -> None:
    vendor(
        orca,
        "Loop",
        {
            "process": [
                ("a", "process/a.json", {"inherits": "b"}),
                ("b", "process/b.json", {"inherits": "a"}),
            ]
        },
    )
    profiles = OrcaProfiles.load(orca)
    with pytest.raises(ModuleError, match="inherits in a loop"):
        profiles.flatten({"name": "x", "inherits": "a"}, "process")


def test_missing_folder_is_empty(tmp_path: Path) -> None:
    profiles = OrcaProfiles.load(tmp_path / "nowhere")
    assert profiles.names("filament") == () and profiles.system == {}


def test_filaments_get_the_keys_their_chain_leaves_out(orca: Path) -> None:
    # RatRig's base filament has no filament_is_support; the CLI needs one per filament.
    profiles = OrcaProfiles.load(orca)
    flat = profiles.flatten(profiles.users["filament"]["PM ASA"], "filament")
    assert flat["filament_is_support"] == ["0"]
    assert flat["chamber_temperature"] == ["0"]  # filled under its current name
    assert flat["filament_density"] == ["1.1"]  # RatRig's value kept, never replaced
    assert "setting_id" not in flat and flat["name"] == "PM ASA"
    # Processes and machines are left as their chain makes them.
    machine = profiles.flatten(profiles.users["machine"]["JoshPrint 0.5 MMU"], "machine")
    assert "filament_is_support" not in machine
