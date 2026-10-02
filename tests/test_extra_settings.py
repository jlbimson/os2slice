"""Extra slicer settings offered in the panel (extra_settings.py, [panel] in the config)."""

from __future__ import annotations

from pathlib import Path

import pytest

from os2slice import config, extra_settings
from os2slice.errors import BadRequest, ConfigError
from os2slice.settings import PrintSettings


def test_values_are_checked_and_written_as_profiles_store_them() -> None:
    by = extra_settings.BY_KEY
    assert by["chamber_temperature"].parse(" 50 ") == "50"
    assert by["layer_height"].parse("0.28") == "0.28"
    assert by["layer_height"].parse("1") == "1"
    assert by["only_one_wall_top"].parse("1") == "1"
    assert by["ironing_type"].parse("topmost") == "topmost"
    for key, bad in (
        ("chamber_temperature", "120"),
        ("chamber_temperature", "warm"),
        ("nozzle_temperature", "250.5"),
        ("layer_height", "0.01"),
        ("only_one_wall_top", "yes"),
        ("seam_position", "everywhere"),
    ):
        with pytest.raises(BadRequest):
            by[key].parse(bad)


def test_overrides_split_by_scope_and_bed_temperature_follows_the_plate() -> None:
    extras = (("chamber_temperature", "50"), ("bed_temperature", "105"), ("layer_height", "0.28"))
    assert extra_settings.overrides(extras, "process") == {"layer_height": "0.28"}
    assert extra_settings.overrides(extras, "filament", "Textured PEI Plate") == {
        "chamber_temperature": "50",
        "textured_plate_temp": "105",
        "textured_plate_temp_initial_layer": "105",
    }
    # No plate known: the bed temperature can't be placed, the rest still applies.
    assert extra_settings.overrides(extras, "filament") == {"chamber_temperature": "50"}


def test_parse_form_takes_only_enabled_filled_fields() -> None:
    form = {"x_chamber_temperature": "50", "x_layer_height": "", "x_seam_position": "back"}
    enabled = ("chamber_temperature", "layer_height")
    assert extra_settings.parse_form(form, enabled) == (("chamber_temperature", "50"),)


def test_print_settings_carry_extras() -> None:
    s = PrintSettings(walls=3, extras=(("layer_height", "0.28"), ("chamber_temperature", "50")))
    assert s.process_overrides()["layer_height"] == "0.28"
    assert "chamber_temperature" not in s.process_overrides()
    assert s.filament_overrides() == {"chamber_temperature": "50"}
    assert s.describe().endswith("; layer height 0.28 mm, chamber temperature 50 °C")
    with pytest.raises(BadRequest):
        PrintSettings(extras=(("chamber_temperature", "500"),))
    with pytest.raises(BadRequest, match="Unknown extra setting"):
        PrintSettings(extras=(("rm -rf", "1"),))


def test_config_panel_table(tmp_path: Path) -> None:
    cfg = config.parse(
        {"panel": {"extra_settings": ["layer_height", "chamber_temperature"]}}, tmp_path / "c"
    )
    assert cfg.panel_extras == ("chamber_temperature", "layer_height")  # catalog order
    assert config.parse({}, tmp_path / "c").panel_extras == ()
    with pytest.raises(ConfigError, match="Unknown extra setting 'warp_speed'"):
        config.parse({"panel": {"extra_settings": ["warp_speed"]}}, tmp_path / "c")
    with pytest.raises(ConfigError, match=r"unknown key\(s\) in \[panel\]"):
        config.parse({"panel": {"extras": []}}, tmp_path / "c")
