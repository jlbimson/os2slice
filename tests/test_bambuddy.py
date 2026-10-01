from __future__ import annotations

import httpx
import pytest

from os2slice.bambuddy import BambuddyClient, BambuddyError, PresetChoice
from os2slice.errors import AuthError
from os2slice.settings import PrintSettings
from tests.fakes import FakeBambuddy

PRESETS = PresetChoice(
    "Bambu Lab A1 mini 0.4 nozzle", "0.20mm Standard @BBL A1M", "Bambu PLA Basic @BBL A1M"
)


def client(handler) -> BambuddyClient:
    return BambuddyClient(
        "http://bambuddy.test:8000/", "bb_key", transport=httpx.MockTransport(handler)
    )


def test_key_header_and_base_path() -> None:
    fake = FakeBambuddy()
    printers = client(fake).list_printers()
    assert [p.name for p in printers] == ["A1 Mini", "X1C_01", "Old", "H2D_01"]
    assert printers[-1].nozzle_count == 2
    req = fake.requests[0]
    assert req.url == "http://bambuddy.test:8000/api/v1/printers/"
    assert req.headers["X-API-Key"] == "bb_key"


def test_ensure_folder_reuses_or_creates() -> None:
    fake = FakeBambuddy()
    c = client(fake)
    root = c.ensure_folder("Onshape")
    doc = c.ensure_folder("test", root)
    assert (root, doc) == (1, 2)
    assert c.ensure_folder("Onshape") == 1 and c.ensure_folder("test", root) == 2
    assert len(fake.folders) == 2


def test_upload_slice_and_wait() -> None:
    fake = FakeBambuddy()
    c = client(fake)
    assert c.upload(1, "Part 1.stl", b"solid") == 30
    job = c.start_slice(30, PRESETS, PrintSettings(3, 25, "tree"), auto_orient=False)
    assert fake.slice_bodies[0] == {
        "printer_preset": {"source": "standard", "id": "Bambu Lab A1 mini 0.4 nozzle"},
        "process_preset": {"source": "standard", "id": "0.20mm Standard @BBL A1M"},
        "filament_preset": {"source": "standard", "id": "Bambu PLA Basic @BBL A1M"},
        "process_overrides": {
            "wall_loops": 3,
            "sparse_infill_density": "25%",
            "enable_support": 1,
            "support_type": "tree(auto)",
            "support_on_build_plate_only": 0,
        },
        "export_3mf": True,
        "auto_orient": False,
        "auto_arrange": True,
    }
    seen: list[str] = []
    result = c.wait_for_slice(job, on_status=seen.append, sleep=lambda s: None)
    assert seen == ["pending", "running", "completed"]
    assert (result.library_file_id, result.print_time_seconds) == (31, 709)


def test_slice_failure_and_timeout() -> None:
    c = client(FakeBambuddy(job_states=["failed"]))
    with pytest.raises(BambuddyError, match="slicer crashed"):
        c.wait_for_slice(7, sleep=lambda s: None)
    c = client(FakeBambuddy(job_states=["running"]))
    with pytest.raises(BambuddyError, match="too long"):
        c.wait_for_slice(7, timeout=-1, sleep=lambda s: None)


def test_queue_print_body() -> None:
    fake = FakeBambuddy()
    item = client(fake).queue_print(31, 1, manual_start=True)
    assert fake.queued == [{"library_file_id": 31, "printer_id": 1, "manual_start": True}]
    assert item["id"] == 99


@pytest.mark.parametrize(
    ("status", "error"),
    [(401, AuthError), (403, AuthError), (500, BambuddyError), (429, BambuddyError)],
)
def test_http_errors(status: int, error: type[Exception]) -> None:
    with pytest.raises(error):
        client(lambda r: httpx.Response(status, json={"detail": "nope"})).list_printers()


def test_unreachable() -> None:
    def boom(r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(BambuddyError, match="Can't reach BamBuddy"):
        client(boom).list_printers()


def test_redirects_are_not_followed() -> None:
    c = client(lambda r: httpx.Response(302, headers={"Location": "http://evil.example/"}))
    with pytest.raises(BambuddyError, match="302"):
        c.list_printers()
