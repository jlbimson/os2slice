from __future__ import annotations

import os
from pathlib import Path

import pytest

from os2slice import addon, auth, config

OPTS = {
    "onshape_access_key": "acc",
    "onshape_secret_key": "sec",
    "bambuddy_api_key": "bb_x",
    "bambuddy_url": "http://172.30.32.1:8000",
    "hosts": ["print.example.duckdns.org:8443"],
    "library_folder": "Onshape",
    "default_printer": "A1 Mini",
    "manual_start": True,
    "walls": 3,
    "infill": 20,
    "supports": "tree",
    "build_plate_only": False,
    "presets": [
        {
            "model": "A1 Mini",
            "printer": "Bambu Lab A1 mini 0.4 nozzle",
            "process": "0.20mm Standard @BBL A1M",
            "filament": "Bambu PLA Basic @BBL A1M",
        },
        {"model": 'Odd "model"', "printer": "p\\x", "process": "q", "filament": "r"},
    ],
    "log_level": "info",
}


def test_prepare_renders_a_valid_lan_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for env in (e for mode in addon.SECRET_OPTIONS.values() for e in mode.values()):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr(os, "environ", os.environ.copy())  # prepare() writes to it
    path = addon.prepare(OPTS, data=tmp_path, ssl_dir=Path("/ssl"))
    assert path == tmp_path / "os2slice" / "config.toml"
    cfg = config.load(path, create=False)
    assert cfg.server.identity == "lan" and cfg.server.bind == "0.0.0.0"
    assert cfg.server.hosts == ("print.example.duckdns.org:8443",)
    assert cfg.server.tls_cert == Path("/ssl/fullchain.pem")
    assert cfg.bambuddy is not None and cfg.bambuddy.base_url == "http://172.30.32.1:8000"
    assert cfg.bambuddy.presets['Odd "model"'].printer == "p\\x"
    assert (cfg.print_defaults.walls, cfg.print_defaults.supports) == (3, "tree")
    assert os.environ["XDG_CONFIG_HOME"] == str(tmp_path)
    assert auth.load_keys().access == "acc"  # from the environment, not a file
    assert "acc" not in path.read_text() and "bb_x" not in path.read_text()


@pytest.mark.parametrize(
    "missing", ["onshape_access_key", "onshape_secret_key", "bambuddy_api_key"]
)
def test_missing_secrets_are_named(tmp_path: Path, missing: str) -> None:
    with pytest.raises(addon.OptionsError, match=missing):
        addon.prepare({**OPTS, missing: "  "}, data=tmp_path)


def test_hosts_required(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "environ", os.environ.copy())
    with pytest.raises(addon.OptionsError, match="hosts"):
        addon.prepare({**OPTS, "hosts": []}, data=tmp_path)


def test_addon_manifest_matches_the_entry_point() -> None:
    root = Path(__file__).resolve().parent.parent
    manifest = (root / "addon/os2slice/config.yaml").read_text()
    assert "8443/tcp: 8443" in manifest and f"{addon.PORT}/tcp" in manifest
    assert "host_network" not in manifest  # D-12: own network namespace
    secrets_ = {k for mode in addon.SECRET_OPTIONS.values() for k in mode}
    for key in (*secrets_, "onshape_auth", "onshape_oauth_client_id", "hosts", "presets",
                "bambuddy_url"):  # fmt: skip
        assert f"  {key}:" in manifest
    assert 'CMD ["os2slice-addon"]' in (root / "addon/os2slice/Dockerfile").read_text()


def test_default_plate_option(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "environ", os.environ.copy())
    cfg = config.load(
        addon.prepare({**OPTS, "default_plate": "Cool Plate"}, data=tmp_path), create=False
    )
    assert cfg.default_bed_type == "Cool Plate"
    cfg = config.load(addon.prepare(OPTS, data=tmp_path), create=False)
    assert cfg.default_bed_type is None


def test_web_studio_option(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "environ", os.environ.copy())
    opts = {**OPTS, "web_studio_url": "https://print.example.duckdns.org:3001"}
    cfg = config.load(addon.prepare(opts, data=tmp_path), create=False)
    assert cfg.web_studio is not None
    assert cfg.web_studio.url == "https://print.example.duckdns.org:3001"
    assert cfg.web_studio.inbox == Path("/share/os2slice/inbox")
    assert config.load(addon.prepare(OPTS, data=tmp_path), create=False).web_studio is None


def test_oauth_mode_needs_its_client_and_skips_api_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "environ", os.environ.copy())
    opts = {k: v for k, v in OPTS.items() if not k.startswith("onshape_")}
    opts["onshape_auth"] = "oauth"
    with pytest.raises(addon.OptionsError, match="onshape_oauth_client_id"):
        addon.prepare(opts, data=tmp_path, ssl_dir=Path("/ssl"))
    opts |= {"onshape_oauth_client_id": "ABCDEFGHIJKL", "onshape_oauth_client_secret": "s3cret"}
    path = addon.prepare(opts, data=tmp_path, ssl_dir=Path("/ssl"))
    cfg = config.load(path, create=False)
    assert cfg.onshape_auth == "oauth" and cfg.oauth_client_id == "ABCDEFGHIJKL"
    assert cfg.oauth_redirect_uri == "https://print.example.duckdns.org:8443/auth/callback"
    assert os.environ["ONSHAPE_OAUTH_CLIENT_SECRET"] == "s3cret"
    assert "s3cret" not in path.read_text()  # secrets never land in the config file
