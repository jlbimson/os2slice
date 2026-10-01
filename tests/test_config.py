from __future__ import annotations

from pathlib import Path

import pytest

from os2slice import config
from os2slice.errors import ConfigError

ROOT = Path(__file__).resolve().parent.parent


def test_first_run_writes_the_default(tmp_path: Path) -> None:
    cfg = config.load()
    assert cfg.path == tmp_path / "config" / "os2slice" / "config.toml"
    assert cfg.path.read_text() == config.default_config_text()
    assert cfg.onshape_base_url == "https://cad.onshape.com"
    assert cfg.port == 8765
    assert cfg.export_dir == Path.home() / "OnshapeExports"
    assert set(cfg.slicers) == {"orca", "bambu", "prusa"}


def test_packaged_default_matches_the_example() -> None:
    assert (ROOT / "os2slice.example.toml").read_text() == config.default_config_text()


def test_load_without_create(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="No config file"):
        config.load(tmp_path / "missing.toml", create=False)


def test_bad_toml(tmp_path: Path) -> None:
    p = tmp_path / "c.toml"
    p.write_text("[export\n")
    with pytest.raises(ConfigError, match="Can't read"):
        config.load(p)


def _parse(data: dict) -> config.Config:
    return config.parse(data, Path("/x/config.toml"))


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"onshape": {"base_url": "https://evil.example"}}, "base_url"),
        ({"onshape": {"base_url": "http://cad.onshape.com"}}, "base_url"),
        ({"onshape": {"base_url": "https://cad.onshape.com.evil.io"}}, "base_url"),
        ({"server": {"port": 80}}, "server.port"),
        ({"server": {"port": "8765"}}, "server.port"),
        ({"server": {"port": True}}, "server.port"),
        ({"export": {"dir": "relative/dir"}}, "absolute"),
        ({"export": {"format": "obj"}}, "export.format"),
        ({"export": {"units": "furlong"}}, "export.units"),
        ({"export": {"keep_days": -1}}, "keep_days"),
        ({"export": {"dirr": "~/x"}}, "unknown key"),
        ({"exports": {}}, "unknown table"),
        ({"slicers": {"Orca": {"argv": ["x"]}}}, "names may only"),
        ({"slicers": {"orca": {"argv": []}}}, "non-empty list"),
        ({"slicers": {"orca": {"argv": "orca-slicer {file}"}}}, "non-empty list"),
        ({"slicers": {"orca": {"argv": ["x", 3]}}}, "non-empty list"),
        ({"slicers": {"orca": "orca-slicer"}}, "must be a table"),
        (
            {
                "bambuddy": {
                    "base_url": "http://bb:8000",
                    "presets": {
                        "X1C": {
                            "printer": "p",
                            "process": "q",
                            "filament": "r",
                            "bed_type": "Glass",
                        }
                    },
                }
            },
            "bed_type",
        ),
    ],
)
def test_invalid_config(data: dict, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        _parse(data)


def test_trailing_slash_and_name_default() -> None:
    cfg = _parse(
        {"onshape": {"base_url": "https://acme.onshape.com/"}, "slicers": {"x": {"argv": ["x"]}}}
    )
    assert cfg.onshape_base_url == "https://acme.onshape.com"
    assert cfg.slicers["x"].name == "x"
