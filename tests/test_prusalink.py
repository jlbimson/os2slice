from __future__ import annotations

import os
import time
from typing import Any

import httpx
import pytest

from os2slice.errors import ConfigError
from os2slice.modules.base import ModuleAuthError, ModuleError, PrinterInfo, SliceOutput, Target
from os2slice.modules.prusalink import SPEC, PrusaLink, file_name
from tests.fakes_printers import PRUSALINK_URL, FakePrusaLink

NOW = time.mktime((2026, 10, 1, 14, 30, 5, 0, 0, -1))
STAMP = "20261001-143005"
PRINTER = PrinterInfo(
    key="mk4", name="MK4", technology="fdm", model="", target="mk4", slicer="prusa"
)
BGCODE = SliceOutput(data=b"GCDE\x01\x00", filename="Bracket v2.bgcode", media="bgcode")


def make(fake: FakePrusaLink, **values: Any) -> PrusaLink:
    values = {"url": PRUSALINK_URL, "api_key": "secret-key", **values}
    return PrusaLink(values, transport=httpx.MockTransport(fake), clock=lambda: NOW)


def test_spec_and_protocol() -> None:
    assert SPEC.kind == "prusalink"
    assert SPEC.accepts == ("gcode", "bgcode")
    fields = {f.key: f for f in SPEC.fields}
    assert set(fields) == {"url", "api_key", "storage", "folder"}
    assert fields["api_key"].type == "secret" and fields["api_key"].required
    assert fields["storage"].choices == ("usb", "local")
    assert isinstance(make(FakePrusaLink()), Target)


def test_config_refusals() -> None:
    with pytest.raises(ConfigError, match="API key"):
        PrusaLink({"url": PRUSALINK_URL})
    with pytest.raises(ConfigError, match="storage"):
        PrusaLink({"url": PRUSALINK_URL, "api_key": "k", "storage": "sd"})
    with pytest.raises(ConfigError):
        PrusaLink({"url": "http://maker:pw@mk4.test", "api_key": "k"})


def test_check() -> None:
    fake = FakePrusaLink()
    h = make(fake).check()
    assert h.ok
    assert h.summary == "PrusaLink 6.1.3+8215 on MK4 left"
    assert [r.url.path for r in fake.requests] == ["/api/version", "/api/v1/info"]


def test_check_bad_key() -> None:
    h = make(FakePrusaLink(), api_key="wrong").check()
    assert not h.ok
    assert "refused the API key" in h.summary


def test_printers_fill_model_and_nozzle() -> None:
    (p,) = make(FakePrusaLink()).printers((PRINTER,))
    assert p.model == "prusa-mk4"
    assert p.extra["nozzle_diameter"] == 0.4
    assert p.ui_url == PRUSALINK_URL


@pytest.mark.parametrize(
    ("state", "connected", "ready"),
    [
        ("IDLE", True, True),
        ("READY", True, True),
        ("FINISHED", True, True),
        ("STOPPED", True, True),
        ("BUSY", True, False),
        ("PRINTING", True, False),
        ("PAUSED", True, False),
        ("ATTENTION", True, False),
        ("ERROR", False, False),
    ],
)
def test_status_mapping(state: str, connected: bool, ready: bool) -> None:
    s = make(FakePrusaLink(state=state)).status(PRINTER)
    assert (s.state, s.connected, s.ready) == (state, connected, ready)
    assert s.materials == ()


def test_status_printing_detail() -> None:
    s = make(FakePrusaLink(state="PRINTING")).status(PRINTER)
    assert s.detail == "42%, 1 h 02 min left, nozzle 215/215 °C, bed 60/60 °C"


def test_status_attention_detail() -> None:
    assert make(FakePrusaLink(state="ATTENTION")).status(PRINTER).detail == (
        "needs attention at the printer"
    )


def test_status_printer_link_down() -> None:
    s = make(FakePrusaLink(state="IDLE", printer_ok=False)).status(PRINTER)
    assert not s.connected and not s.ready
    assert s.detail == "Printer not connected"


def test_status_offline() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    p = PrusaLink({"url": PRUSALINK_URL, "api_key": "k"}, transport=httpx.MockTransport(boom))
    s = p.status(PRINTER)
    assert (s.state, s.connected, s.ready) == ("offline", False, False)


def test_submit_waiting() -> None:
    fake = FakePrusaLink()
    sub = make(fake).submit(PRINTER, BGCODE, start=False)
    (req,) = fake.uploads
    assert req.method == "PUT"
    assert req.url.path == "/api/v1/files/usb/os2slice/Bracket_v2.bgcode"
    assert req.content == BGCODE.data
    assert req.headers["Content-Type"] == "application/octet-stream"
    assert req.headers["Content-Length"] == str(len(BGCODE.data))
    assert req.headers["Overwrite"] == "?0"
    assert req.headers["Print-After-Upload"] == "?0"
    assert req.headers["X-Api-Key"] == "secret-key"
    assert sub.state == "waiting"
    assert sub.id == "usb/os2slice/Bracket_v2.bgcode"
    assert sub.detail == (
        "Saved on the printer's USB as Bracket_v2.bgcode; "
        "start it from the printer's screen or PrusaLink"
    )
    assert sub.url == PRUSALINK_URL


def test_submit_started_local_storage() -> None:
    fake = FakePrusaLink()
    out = SliceOutput(data=b"G28\n", filename="a.gcode", media="gcode")
    sub = make(fake, storage="local", folder="").submit(PRINTER, out, start=True)
    (req,) = fake.uploads
    assert req.url.path == "/api/v1/files/local/a.gcode"
    assert req.headers["Print-After-Upload"] == "?1"
    assert sub.state == "started"


def test_submit_conflict_makes_name_unique() -> None:
    fake = FakePrusaLink(existing={"usb/os2slice/Bracket_v2.bgcode"})
    sub = make(fake).submit(PRINTER, BGCODE, start=False)
    assert [r.url.path for r in fake.uploads] == [
        "/api/v1/files/usb/os2slice/Bracket_v2.bgcode",
        f"/api/v1/files/usb/os2slice/Bracket_v2_{STAMP}.bgcode",
    ]
    assert all(r.headers["Overwrite"] == "?0" for r in fake.uploads)
    assert sub.id == f"usb/os2slice/Bracket_v2_{STAMP}.bgcode"


def test_submit_second_conflict_fails() -> None:
    fake = FakePrusaLink(
        existing={"usb/os2slice/Bracket_v2.bgcode", f"usb/os2slice/Bracket_v2_{STAMP}.bgcode"}
    )
    with pytest.raises(ModuleError, match=r"error 409.*File already exists"):
        make(fake).submit(PRINTER, BGCODE, start=False)


@pytest.mark.parametrize(("code", "fix"), [(413, "too large"), (507, "full"), (500, "")])
def test_submit_errors(code: int, fix: str) -> None:
    with pytest.raises(ModuleError, match=f"PrusaLink error {code}.*Storage full") as e:
        make(FakePrusaLink(fail_with=code)).submit(PRINTER, BGCODE, start=False)
    assert fix in e.value.fix


@pytest.mark.parametrize("media", ["gcode.3mf", "form"])
def test_submit_refuses_other_media(media: str) -> None:
    fake = FakePrusaLink()
    out = SliceOutput(data=b"x", filename="a", media=media)  # type: ignore[arg-type]
    with pytest.raises(ModuleError, match=r"\.gcode or \.bgcode"):
        make(fake).submit(PRINTER, out, start=False)
    assert fake.requests == []


def test_unauthorized_is_module_error() -> None:
    fake = FakePrusaLink()
    with pytest.raises(ModuleError, match="refused the API key: Unauthorized") as e:
        make(fake, api_key="wrong").submit(PRINTER, BGCODE, start=False)
    assert "API key" in e.value.fix and isinstance(e.value, ModuleAuthError)
    assert "[targets.prusalink]" in e.value.fix
    assert fake.existing == set()


def test_file_names() -> None:
    assert file_name("../../x y.bgcode", ".bgcode") == "x_y.bgcode"
    assert file_name("a%2F#?.gcode", ".gcode") == "a_2F.gcode"
    assert file_name("", ".gcode", STAMP) == f"part_{STAMP}.gcode"


def test_only_configured_host_called() -> None:
    fake = FakePrusaLink()
    p = make(fake)
    p.check()
    p.status(PRINTER)
    p.submit(PRINTER, BGCODE, start=False)
    assert {r.url.host for r in fake.requests} == {"mk4.test"}


LIVE_URL = os.environ.get("OS2SLICE_LIVE_PRUSALINK_URL", "")


@pytest.mark.live
@pytest.mark.skipif(not LIVE_URL, reason="OS2SLICE_LIVE_PRUSALINK_URL not set")
def test_live_prusalink_read_only() -> None:
    values = {"url": LIVE_URL, "api_key": os.environ.get("OS2SLICE_LIVE_PRUSALINK_KEY", "")}
    with PrusaLink(values) as p:
        h = p.check()
        print(h)
        print(p.status(PRINTER))
        assert h.summary
