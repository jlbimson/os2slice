from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx
import pytest

from os2slice.errors import ConfigError
from os2slice.modules.base import ModuleAuthError, ModuleError, PrinterInfo, SliceOutput, Target
from os2slice.modules.moonraker import SPEC, Moonraker, unique_name
from tests.fakes_printers import MOONRAKER_URL, FakeMoonraker

NOW = time.mktime((2026, 10, 1, 14, 30, 5, 0, 0, -1))
STAMP = "20261001-143005"
PRINTER = PrinterInfo(
    key="voron", name="Voron", technology="fdm", model="", target="voron", slicer="orca"
)
GCODE = SliceOutput(data=b"G28\nG1 X10\n", filename="Bracket v2.gcode", media="gcode")


def make(fake: FakeMoonraker, **values: Any) -> Moonraker:
    values = {"url": MOONRAKER_URL, **values}
    return Moonraker(values, transport=httpx.MockTransport(fake), clock=lambda: NOW)


def test_spec_and_protocol() -> None:
    assert SPEC.kind == "moonraker"
    assert SPEC.accepts == ("gcode",)
    assert {f.key for f in SPEC.fields} == {"url", "api_key", "ui_url", "folder", "spoolman"}
    assert next(f for f in SPEC.fields if f.key == "api_key").type == "secret"
    assert isinstance(make(FakeMoonraker()), Target)


@pytest.mark.parametrize(
    "url", ["ftp://voron", "http://user:pw@voron:7125", "voron:7125", "http://voron/?x=1"]
)
def test_bad_url_refused(url: str) -> None:
    with pytest.raises(ConfigError):
        Moonraker({"url": url})


def test_check_ready() -> None:
    h = make(FakeMoonraker()).check()
    assert h.ok
    assert h.version == "v0.9.3-12-gabcdef0"
    assert "Klipper v0.12.0" in h.summary


def test_check_klippy_not_ready() -> None:
    fake = FakeMoonraker(klippy_state="shutdown")
    h = make(fake).check()
    assert not h.ok
    assert "shutdown" in h.summary
    assert [r.url.path for r in fake.requests] == ["/server/info"]


def test_check_unreachable_is_not_ok() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    h = Moonraker({"url": MOONRAKER_URL}, transport=httpx.MockTransport(boom)).check()
    assert not h.ok
    assert "Can't reach Moonraker" in h.summary


def test_printers_fill_model_and_ui_url() -> None:
    fake = FakeMoonraker()
    named = PrinterInfo(
        key="v2", name="V2", technology="fdm", model="Voron 2.4", target="voron", slicer="orca"
    )
    out = make(fake, ui_url="http://voron.test").printers((PRINTER, named))
    assert out[0].model == "voron24"
    assert out[1].model == "Voron 2.4"
    assert all(p.ui_url == "http://voron.test" for p in out)


def test_printers_skip_info_when_all_have_models() -> None:
    fake = FakeMoonraker()
    p = PrinterInfo(key="v", name="V", technology="fdm", model="M", target="t", slicer="s")
    assert make(fake).printers((p,)) == (p,)
    assert fake.requests == []


def test_status_query_shape() -> None:
    fake = FakeMoonraker()
    make(fake).status(PRINTER)
    url = fake.requests[0].url
    assert url.path == "/printer/objects/query"
    assert url.query == b"webhooks&print_stats&virtual_sdcard&extruder&heater_bed&toolhead"


@pytest.mark.parametrize(
    ("state", "active", "ready"),
    [
        ("standby", False, True),
        ("complete", False, True),
        ("cancelled", False, True),
        ("error", False, True),
        ("printing", True, False),
        ("paused", True, False),
        ("error", True, False),
    ],
)
def test_status_mapping(state: str, active: bool, ready: bool) -> None:
    s = make(FakeMoonraker(print_state=state, sd_active=active)).status(PRINTER)
    assert s.state == state
    assert s.connected
    assert s.ready is ready
    assert s.materials == ()


def test_status_printing_detail() -> None:
    s = make(FakeMoonraker(print_state="printing", sd_active=True, progress=0.425)).status(PRINTER)
    assert s.detail == "42%, nozzle 215/215 °C, bed 60/60 °C"


def test_status_error_message() -> None:
    s = make(FakeMoonraker(print_state="error", message="Move out of range")).status(PRINTER)
    assert s.detail == "Move out of range"


def test_status_klippy_down() -> None:
    s = make(FakeMoonraker(klippy_state="shutdown")).status(PRINTER)
    assert (s.state, s.connected, s.ready) == ("klipper shutdown", False, False)


def test_status_offline() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout")

    s = Moonraker({"url": MOONRAKER_URL}, transport=httpx.MockTransport(boom)).status(PRINTER)
    assert (s.state, s.connected, s.ready) == ("offline", False, False)


def test_spoolman_material() -> None:
    fake = FakeMoonraker(spool_id=7)
    s = make(fake, spoolman=True).status(PRINTER)
    (m,) = s.materials
    assert m.id == "7"
    assert m.kind == "PLA"
    assert m.colour == "#BD0B0B"
    assert m.label == "Spool 7: PLA, Fusion Reactor Red"
    proxy = next(r for r in fake.requests if r.url.path == "/server/spoolman/proxy")
    assert json.loads(proxy.content) == {
        "use_v2_response": True,
        "request_method": "GET",
        "path": "/v1/spool/7",
    }


def test_spoolman_no_active_spool() -> None:
    fake = FakeMoonraker(spool_id=None)
    assert make(fake, spoolman=True).status(PRINTER).materials == ()
    assert not any(r.url.path == "/server/spoolman/proxy" for r in fake.requests)


def test_spoolman_error_is_not_fatal() -> None:
    s = make(FakeMoonraker(spool_id=3, spool_v2_error=True), spoolman=True).status(PRINTER)
    assert s.materials == ()
    assert s.ready


def test_spoolman_off_by_default() -> None:
    fake = FakeMoonraker(spool_id=7)
    assert make(fake).status(PRINTER).materials == ()
    assert not any("spoolman" in r.url.path for r in fake.requests)


def test_submit_waiting() -> None:
    fake = FakeMoonraker()
    sub = make(fake, ui_url="http://voron.test").submit(PRINTER, GCODE, start=False)
    (form,) = fake.uploads
    name = f"Bracket_v2_{STAMP}.gcode"
    assert form == {"root": "gcodes", "path": "os2slice", "file_name": name, "file": GCODE.data}
    assert "print" not in form
    assert sub.state == "waiting"
    assert sub.id == f"os2slice/{name}"
    assert sub.detail == f"Uploaded to os2slice/{name}; start it from Mainsail or Fluidd"
    assert sub.url == "http://voron.test"


def test_submit_started() -> None:
    fake = FakeMoonraker()
    sub = make(fake, folder="Shop jobs").submit(PRINTER, GCODE, start=True)
    (form,) = fake.uploads
    assert form["print"] == "true"
    assert form["path"] == "Shop_jobs"
    assert sub.state == "started"


def test_submit_start_refused_by_moonraker_stays_waiting() -> None:
    sub = make(FakeMoonraker(print_started=False)).submit(PRINTER, GCODE, start=True)
    assert sub.state == "waiting"


def test_unique_names() -> None:
    assert unique_name("../../etc/passwd", ".gcode", NOW) == f"etc_passwd_{STAMP}.gcode"
    assert unique_name("Ünïcode part.gcode", ".gcode", NOW) == f"Unicode_part_{STAMP}.gcode"
    assert unique_name("", ".gcode", NOW) == f"part_{STAMP}.gcode"
    a = unique_name("x.gcode", ".gcode", NOW)
    b = unique_name("x.gcode", ".gcode", NOW + 1)
    assert a != b


@pytest.mark.parametrize("media", ["gcode.3mf", "bgcode", "form"])
def test_submit_refuses_other_media(media: str) -> None:
    fake = FakeMoonraker()
    out = SliceOutput(data=b"x", filename="a", media=media)  # type: ignore[arg-type]
    with pytest.raises(ModuleError, match="plain G-code"):
        make(fake).submit(PRINTER, out, start=False)
    assert fake.requests == []


def test_api_key_header_sent() -> None:
    fake = FakeMoonraker(api_key="k123")
    assert make(fake, api_key="k123").check().ok
    assert all(r.headers["X-Api-Key"] == "k123" for r in fake.requests)


def test_no_api_key_header_without_key() -> None:
    fake = FakeMoonraker()
    make(fake).check()
    assert all("X-Api-Key" not in r.headers for r in fake.requests)


def test_unauthorized_is_module_error() -> None:
    fake = FakeMoonraker(api_key="k123")
    with pytest.raises(ModuleError, match=r"refused the request \(401: Unauthorized\)") as e:
        make(fake).submit(PRINTER, GCODE, start=False)
    assert "API key" in e.value.fix
    assert isinstance(e.value, ModuleAuthError) and e.value.http_status == 502
    assert fake.uploads == []
    with pytest.raises(ModuleAuthError):  # status() raises for auth, it isn't "offline"
        make(fake).status(PRINTER)


def test_server_error_is_module_error() -> None:
    with pytest.raises(ModuleError, match="Moonraker error 500 on /server/files/upload"):
        make(FakeMoonraker(fail_with=500)).submit(PRINTER, GCODE, start=False)


def test_only_configured_host_called() -> None:
    fake = FakeMoonraker(spool_id=7)
    m = make(fake, spoolman=True)
    m.check()
    m.status(PRINTER)
    m.submit(PRINTER, GCODE, start=False)
    assert {r.url.host for r in fake.requests} == {"voron.test"}
    assert {r.url.port for r in fake.requests} == {7125}


LIVE_URL = os.environ.get("OS2SLICE_LIVE_MOONRAKER_URL", "")


@pytest.mark.live
@pytest.mark.skipif(not LIVE_URL, reason="OS2SLICE_LIVE_MOONRAKER_URL not set")
def test_live_moonraker_read_only() -> None:
    values = {"url": LIVE_URL, "api_key": os.environ.get("OS2SLICE_LIVE_MOONRAKER_KEY", "")}
    with Moonraker(values) as m:
        h = m.check()
        print(h)
        print(m.status(PRINTER))
        assert h.summary
