from __future__ import annotations

import struct
from pathlib import Path

import pytest

from os2slice import config, notify

DOC = "0123456789abcdef01234567"
WS = "89abcdef0123456789abcdef"
ELEM = "fedcba9876543210fedcba98"

# Shaped exactly like what Onshape sent to the spike listener (docs/ONSHAPE_API.md),
# with IDs replaced by fakes.
ONSHAPE_EXTRAS = (
    "&companyId=aaaaaaaaaaaaaaaaaaaaaaaa&sessionCompanyId=cad"
    "&server=https%3A%2F%2Fcad.onshape.com&userId=bbbbbbbbbbbbbbbbbbbbbbbb"
    "&clientId=FAKECLIENTID%3D&locale=en-US&theme=dark"
)
QUERY_NO_CONFIG = (
    f"slicer=orca&d={DOC}&wv=w&wvid={WS}&e={ELEM}&p=JHD&c=%7B$configuration%7D" + ONSHAPE_EXTRAS
)
QUERY_WITH_CONFIG = (
    f"slicer=orca&d={DOC}&wv=w&wvid={WS}&e={ELEM}&p=JHD&c=List_zrSB7lcyzQWXqq%3D_1" + ONSHAPE_EXTRAS
)

SLICERS = ("orca", "bambu", "prusa")


def make_stl(triangles: int = 12) -> bytes:
    body = struct.pack("<12fH", *([0.0] * 12), 0) * triangles
    return b"\0" * 80 + struct.pack("<I", triangles) + body


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never touch the real config, log, keyring or desktop."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("ONSHAPE_ACCESS_KEY", raising=False)
    monkeypatch.delenv("ONSHAPE_SECRET_KEY", raising=False)
    monkeypatch.setattr("keyring.get_password", lambda *a: None)
    monkeypatch.setattr("keyring.set_password", lambda *a: None)
    # A usable (fake) keyring, so the file store is off unless a test turns it on.
    monkeypatch.setattr("os2slice.auth._backend", lambda: object())
    monkeypatch.delenv("OS2SLICE_ADDON", raising=False)
    monkeypatch.delenv("PYTHON_KEYRING_BACKEND", raising=False)


@pytest.fixture(autouse=True)
def notifications(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, bool]]:
    sent: list[tuple[str, str, bool]] = []
    monkeypatch.setattr(notify, "notify", lambda s, b="", error=False: sent.append((s, b, error)))
    return sent


@pytest.fixture
def cfg(tmp_path: Path) -> config.Config:
    data = {
        "export": {"dir": str(tmp_path / "exports"), "keep_days": 30},
        "slicers": {
            "orca": {"name": "OrcaSlicer", "argv": ["orca-slicer", "{file}"]},
            "bambu": {"name": "Bambu Studio", "argv": ["bambu-studio"]},
            "prusa": {
                "name": "PrusaSlicer",
                "argv": [
                    "flatpak",
                    "run",
                    "--file-forwarding",
                    "com.prusa3d.PrusaSlicer",
                    "--single-instance",
                    "@@",
                    "{file}",
                    "@@",
                ],
            },
        },
        "bambuddy": {
            "base_url": "http://bambuddy.test:8000",
            "default_printer": "A1 Mini",
            "presets": {
                "A1 Mini": {
                    "printer": "Bambu Lab A1 mini 0.4 nozzle",
                    "process": "0.20mm Standard @BBL A1M",
                    "filament": "Bambu PLA Basic @BBL A1M",
                }
            },
        },
    }
    return config.parse(data, tmp_path / "config.toml")
