"""The OrcaSlicer / Bambu Studio API sidecar module, against tests/fakes_slicerapi.py.

Live: OS2SLICE_LIVE_SLICERAPI_URL=http://127.0.0.1:3001 pytest -q -m live tests/test_slicerapi.py
(slices only; nothing here can print).
"""

from __future__ import annotations

import io
import os
import re
import threading
import zipfile
from itertools import pairwise
from pathlib import Path
from typing import Any

import httpx
import pytest

from os2slice.modules.base import (
    Material,
    ModuleAuthError,
    ModuleError,
    PartGeometry,
    PrinterInfo,
    Profiles,
    SliceInput,
    Slicer,
)
from os2slice.modules.slicerapi import (
    MODULES,
    SPECS,
    BambuStudioApi,
    OrcaSlicerApi,
    SlicerApi,
)
from os2slice.orientation import bounding_box
from os2slice.settings import PrintSettings
from tests.fakes_slicerapi import (
    A1M,
    AFK_NAMES,
    REQUEST_ID,
    RESOLVER_HEALTH,
    FakeSidecar,
    box_stl,
    gcode,
    gcode_3mf,
)

URL = "http://172.30.32.1:3001"
PROFILES = Profiles(A1M, "0.20mm Standard @BBL A1M", "Bambu PLA Basic @BBL A1M")
A1_MINI = PrinterInfo("a1m", "A1 Mini", "fdm", "A1 Mini", "farm", "studio", bed_mm=(180, 180))
H2D = PrinterInfo(
    "h2d", "H2D_01", "fdm", "H2D", "farm", "studio", bed_mm=(350, 320), nozzle_count=2
)
RED = Material("0", "AMS 1 slot 1: PLA, red", "PLA", "#FF0000", 1, "Bambu PLA Basic @BBL H2D")
BLUE = Material("5", "AMS 2 slot 2: PLA, blue", "PLA", "#0000FF", 0, "Bambu PLA Basic @BBL H2D")


def api(fake: FakeSidecar, cls: type[SlicerApi] = BambuStudioApi, **kw: Any) -> SlicerApi:
    module = cls(URL, kw.pop("timeout_s", 60), kw.pop("api_key", ""), transport=fake.transport())
    module.poll_s = 0.01
    return module


def job(
    parts: tuple[PartGeometry, ...] | None = None,
    printer: PrinterInfo = A1_MINI,
    settings: PrintSettings | None = None,
    **kw: Any,
) -> SliceInput:
    parts = parts or (PartGeometry("JHD", box_stl(50.8, 25.4, 6.35)),)
    settings = settings or PrintSettings(walls=3, infill=25)
    return SliceInput("Part 1 (JHD)", printer, parts, settings, kw.pop("profiles", PROFILES), **kw)


class Log(list[str]):
    def __call__(self, line: str) -> None:
        self.append(line)


def stub(part: Any) -> dict[str, Any]:
    body = part.json()
    assert part.content_type == "application/json" and part.filename.endswith(".json")
    return body


# -- spec and construction -------------------------------------------------------------


def test_specs_two_kinds_one_class() -> None:
    kinds = {s.kind: s for s in SPECS}
    assert set(kinds) == {"orca-slicer-api", "bambu-studio-api"} == set(MODULES)
    assert "3003" in kinds["orca-slicer-api"].fields[0].help
    assert "3001" in kinds["bambu-studio-api"].fields[0].help
    for spec in SPECS:
        assert spec.role == "slicer" and spec.makes == ("gcode.3mf", "gcode")
        assert {f.key: f.type for f in spec.fields} == {
            "url": "url",
            "timeout_s": "int",
            "api_key": "secret",
        }
        assert MODULES[spec.kind].spec is spec
        assert issubclass(MODULES[spec.kind], SlicerApi)
    assert isinstance(BambuStudioApi(URL), Slicer)
    module = OrcaSlicerApi.from_values({"url": URL + "/", "timeout_s": None})
    assert module.url == URL and module.timeout_s == 900


@pytest.mark.parametrize(
    "url", ["ftp://host:3001", "localhost:3001", "http://h:3001/?x=1", "http://u:p@h:3001"]
)
def test_bad_url_refused(url: str) -> None:
    with pytest.raises(ModuleError):
        BambuStudioApi(url)


def test_api_key_sent_only_when_set() -> None:
    fake = FakeSidecar()
    api(fake).check()
    assert "authorization" not in fake.requests[-1].headers
    api(fake, api_key="s3cret").check()
    assert fake.requests[-1].headers["authorization"] == "Bearer s3cret"


def test_a_proxy_refusing_the_key_is_an_auth_error() -> None:
    fake = FakeSidecar(slice_error=(401, {"message": "Unauthorized"}))
    with pytest.raises(ModuleAuthError, match="HTTP 401") as e:
        api(fake).slice(job(), Log())
    assert "[slicers.bambu-studio-api]" in e.value.fix


# -- check -------------------------------------------------------------------------------


def test_check_healthy() -> None:
    health = api(FakeSidecar("afk"), OrcaSlicerApi).check()
    assert health.ok and health.version == "2.4.2"
    assert "healthy" in health.summary and "2.4.2" in health.summary


def test_check_unhealthy_says_why() -> None:
    body = {**RESOLVER_HEALTH, "checks": {"orcaslicer": {"available": False, "error": "gone"}}}
    health = api(FakeSidecar("resolver", health=(503, body))).check()
    assert not health.ok and "orcaslicer: gone" in health.summary and not health.detail


def test_check_resolver_without_data_path_passes_with_a_warning() -> None:
    health = api(FakeSidecar("resolver")).check()  # the add-on's real answer: 503 unhealthy
    assert health.ok and health.summary == "Bambu Studio API sidecar healthy"
    assert "dataPath" in health.detail and "/app/data" in health.detail


def test_check_not_json_or_unreachable() -> None:
    fake = FakeSidecar(health=(502, "Bad Gateway"))
    assert not api(fake).check().ok

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    module = BambuStudioApi(URL, transport=httpx.MockTransport(refuse))
    health = module.check()
    assert not health.ok and "Can't reach" in health.summary


# -- profiles ----------------------------------------------------------------------------


def test_profiles_resolver_all_and_narrowed() -> None:
    fake = FakeSidecar("resolver")
    module = api(fake)
    everything = module.profiles()
    assert len(everything.printer) == 2 and len(everything.filament) == 2
    a1m = module.profiles("A1 mini")
    assert a1m.printer == (A1M,)
    assert a1m.process == ("0.20mm Standard @BBL A1M", "0.20mm Odd")  # compat, or generic
    assert a1m.filament == ("Bambu PLA Basic @BBL A1M",)
    assert module.profiles(A1M).printer == (A1M,)
    assert fake.paths.count("GET /profiles/bundled") == 4  # probe once, then one per call


def test_profiles_afk_names() -> None:
    fake = FakeSidecar("afk")
    catalog = api(fake, OrcaSlicerApi).profiles("X1 Carbon")
    assert catalog.printer == ("Bambu Lab X1 Carbon 0.4 nozzle",)
    assert catalog.process == tuple(AFK_NAMES["presets"])  # no compatibility data: all
    assert catalog.filament == tuple(AFK_NAMES["filaments"])
    assert "GET /profiles/presets" in fake.paths


# -- slicing: input choice -----------------------------------------------------------


def test_single_part_sends_centred_stl_and_stubs() -> None:
    fake = FakeSidecar("resolver")
    log = Log()
    out = api(fake).slice(job(bed_type="Textured PEI Plate"), log)

    (model,) = fake.files(name="file")
    assert model.filename == "Part_1__JHD.stl" and model.content_type == "model/stl"
    lo, hi = bounding_box(model.data)
    assert (lo[0] + hi[0]) / 2 == pytest.approx(90, abs=0.01)
    assert (lo[1] + hi[1]) / 2 == pytest.approx(90, abs=0.01)

    printer, process = (
        stub(fake.files(name="printerProfile")[0]),
        stub(fake.files(name="presetProfile")[0]),
    )
    assert printer == {"name": A1M, "inherits": A1M, "from": "system", "type": "machine"}
    assert process["inherits"] == "0.20mm Standard @BBL A1M" and process["type"] == "process"
    (filament,) = [stub(p) for p in fake.files(name="filamentProfile")]
    assert filament["inherits"] == "Bambu PLA Basic @BBL A1M" and "filament_colour" not in filament

    fields = fake.fields()
    assert fields["plate"] == "1" and fields["exportType"] == "3mf"
    assert fields["bedType"] == "Textured PEI Plate"
    assert re.fullmatch(r"[0-9a-f-]{36}", fields["requestId"])
    assert "arrange" not in fields and "orient" not in fields  # "false" is truthy upstream
    assert "resolveProfileInheritance" not in fields

    assert out.media == "gcode.3mf" and out.filename == "Part 1 (JHD).gcode.3mf"
    assert (out.print_time_s, out.material_g, out.layers) == (905, 5.88, 32)
    assert out.report["input"] == "stl" and out.report["flavour"] == "resolver"
    assert log[0] == "Uploading to the slicer"
    assert all(r.url.host == "172.30.32.1" and r.url.port == 3001 for r in fake.requests)


def test_auto_flags_and_no_centring_when_arranging() -> None:
    fake = FakeSidecar("resolver")
    stl = box_stl(10, 10, 5, x=3, y=4)
    api(fake).slice(job((PartGeometry("p", stl),), auto_orient=True, auto_arrange=True), Log())
    assert fake.fields()["arrange"] == "true" and fake.fields()["orient"] == "true"
    assert fake.files(name="file")[0].data == stl


def test_overrides_passed_through_as_strings() -> None:
    fake = FakeSidecar("resolver")
    settings = PrintSettings(4, 30, "tree", True, 6, 4, True)
    extra = {"process_overrides": {"wipe_tower_x": "70.0", "wipe_tower_y": "114.7"}}
    api(fake).slice(job(settings=settings, extra=extra), Log())
    process = stub(fake.files(name="presetProfile")[0])
    assert process["wall_loops"] == "4" and process["sparse_infill_density"] == "30%"
    assert process["enable_support"] == "1" and process["support_type"] == "tree(auto)"
    assert process["support_on_build_plate_only"] == "1"
    assert process["top_shell_layers"] == "6" and process["bottom_shell_layers"] == "4"
    assert process["brim_type"] == "outer_only"
    assert process["wipe_tower_x"] == "70.0" and process["wipe_tower_y"] == "114.7"
    assert process["name"] == process["inherits"] == "0.20mm Standard @BBL A1M"


def build_items(threemf: bytes) -> tuple[list[tuple[float, float]], tuple[Any, Any]]:
    with zipfile.ZipFile(io.BytesIO(threemf)) as z:
        top = z.read("3D/3dmodel.model").decode()
        sub = z.read("3D/Objects/object_1.model").decode()
    offsets = [
        (float(m.group(1)), float(m.group(2)))
        for m in re.finditer(r'<item [^>]*transform="1 0 0 0 1 0 0 0 1 (\S+) (\S+) 0"', top)
    ]
    xs = [float(x) for x in re.findall(r'<vertex x="([^"]+)"', sub)]
    ys = [float(y) for y in re.findall(r'<vertex x="[^"]+" y="([^"]+)"', sub)]
    return offsets, ((min(xs), min(ys)), (max(xs), max(ys)))


@pytest.mark.parametrize(("brim", "gap"), [(False, 6.0), (True, 16.0)])
def test_copies_build_a_3mf_grid_on_the_bed(brim: bool, gap: float) -> None:
    fake = FakeSidecar("resolver")
    settings = PrintSettings(brim=brim, copies=4)
    out = api(fake).slice(job(settings=settings, copies=4), Log())
    (model,) = fake.files(name="file")
    assert model.filename.endswith(".3mf") and model.content_type == "model/3mf"
    offsets, ((x0, y0), (x1, y1)) = build_items(model.data)
    assert len(offsets) == 4
    for dx, dy in offsets:  # every copy inside the bed, with the margin
        assert x0 + dx >= 5 and x1 + dx <= 175 and y0 + dy >= 5 and y1 + dy <= 175
    xs = sorted({dx for dx, _ in offsets})
    ys = sorted({dy for _, dy in offsets})
    w, d = x1 - x0, y1 - y0
    assert all(b - a == pytest.approx(w + gap, abs=0.01) for a, b in pairwise(xs))
    assert all(b - a == pytest.approx(d + gap, abs=0.01) for a, b in pairwise(ys))
    assert out.report["input"] == "3mf"
    assert len(fake.files(name="filamentProfile")) == 1


def test_too_many_copies_refused_before_upload() -> None:
    fake = FakeSidecar("resolver")
    big = (PartGeometry("big", box_stl(100, 100, 10)),)
    with pytest.raises(ModuleError, match="don't fit"):
        api(fake).slice(job(big, settings=PrintSettings(copies=4), copies=4), Log())
    assert not fake.uploads


def test_multi_material_3mf_with_colours() -> None:
    fake = FakeSidecar("resolver")
    red = Material("0", "red", "PLA", "#FF0000", None, "Bambu PLA Basic @BBL A1M")
    blue = Material("5", "blue", "PLA", "#0000FF", None, "")
    parts = (
        PartGeometry("base", box_stl(40, 20, 5), red),
        PartGeometry("text", box_stl(10, 5, 1, x=5, y=5), blue),
        PartGeometry("more red", box_stl(5, 5, 5, x=30), red),
    )
    out = api(fake).slice(job(parts), Log())
    filaments = [stub(p) for p in fake.files(name="filamentProfile")]
    assert [f["filament_colour"] for f in filaments] == [["#FF0000"], ["#0000FF"]]
    # blue has no matching profile: the job's default filament profile
    assert [f["inherits"] for f in filaments] == ["Bambu PLA Basic @BBL A1M"] * 2
    with zipfile.ZipFile(io.BytesIO(fake.files(name="file")[0].data)) as z:
        settings = z.read("Metadata/model_settings.config").decode()
    assert re.findall(r'key="extruder" value="(\d)"', settings)[1:] == ["1", "2", "1"]
    assert "filament_maps" not in settings  # single nozzle: nothing pinned
    assert out.report["filaments"] == ["Bambu PLA Basic @BBL A1M"] * 2


def test_dual_nozzle_pins_filaments() -> None:
    fake = FakeSidecar("resolver")
    profiles = Profiles("Bambu Lab H2D 0.4 nozzle", "0.20mm Standard @BBL H2D", "x")
    parts = (PartGeometry("base", box_stl(40, 20, 5), RED),)
    api(fake).slice(job(parts, H2D, profiles=profiles), Log())
    with zipfile.ZipFile(io.BytesIO(fake.files(name="file")[0].data)) as z:
        settings = z.read("Metadata/model_settings.config").decode()
    assert 'key="filament_maps" value="1"' in settings  # one part, but pinned: 3MF
    parts = (PartGeometry("a", box_stl(40, 20, 5), RED), PartGeometry("b", box_stl(9, 9, 9), BLUE))
    api(fake).slice(job(parts, H2D, profiles=profiles), Log())
    with zipfile.ZipFile(io.BytesIO(fake.files(name="file")[0].data)) as z:
        assert 'value="1 2"' in z.read("Metadata/model_settings.config").decode()


def test_afk_refuses_multi_filament() -> None:
    fake = FakeSidecar("afk")
    red = Material("0", "red", "PLA", "#FF0000")
    blue = Material("5", "blue", "PLA", "#0000FF")
    parts = (PartGeometry("a", box_stl(9, 9, 9), red), PartGeometry("b", box_stl(9, 9, 9), blue))
    with pytest.raises(ModuleError, match="one filament"):
        api(fake, OrcaSlicerApi).slice(job(parts), Log())
    assert not fake.uploads


def test_missing_profiles_refused() -> None:
    with pytest.raises(ModuleError, match="profile"):
        api(FakeSidecar()).slice(job(profiles=Profiles(A1M, "", "x")), Log())
    with pytest.raises(ModuleError, match="filament"):
        api(FakeSidecar()).slice(job(profiles=Profiles(A1M, "p", "")), Log())


# -- slicing: async (afk) and sync + progress (resolver) ------------------------------


def test_afk_async_polls_downloads_and_tidies() -> None:
    fake = FakeSidecar("afk")
    log = Log()
    out = api(fake, OrcaSlicerApi).slice(job(), log)
    status = f"/slice-async/{REQUEST_ID}"
    assert fake.paths == [
        "GET /profiles/bundled",
        "POST /slice-async",
        f"GET {status}",
        f"GET {status}",
        f"GET {status}",
        f"GET {status}/result",
        f"DELETE {status}",
    ]
    assert fake.fields()["resolveProfileInheritance"] == "true"
    assert "requestId" not in fake.fields()
    assert log == [
        "Uploading to the slicer",
        "Slicing (pending)",
        "Slicing (processing)",
        "Downloading the sliced file",
    ]
    assert (out.print_time_s, out.material_g, out.layers) == (905, 5.88, 32)
    assert out.report["request_id"] == REQUEST_ID


def test_afk_async_failure_names_unknown_profile() -> None:
    fake = FakeSidecar(
        "afk",
        statuses=["processing", "failed"],
        fail_message="Failed to prepare slicing",
        unknown_profiles={"0.20mm Standard @BBL A1M"},
    )
    with pytest.raises(ModuleError) as e:
        api(fake, OrcaSlicerApi).slice(job(), Log())
    assert "no profile named '0.20mm Standard @BBL A1M'" in e.value.message
    assert b"/profiles/presets/0.20mm%20Standard%20%40BBL%20A1M" in [
        r.url.raw_path for r in fake.requests
    ]


def test_afk_async_failure_keeps_slicer_reason() -> None:
    fake = FakeSidecar("afk", statuses=["failed"], fail_message="Slicing failed: no space")
    with pytest.raises(ModuleError, match=r"^Slicing failed: no space$"):
        api(fake, OrcaSlicerApi).slice(job(), Log())


def test_afk_async_bad_request_id_not_followed() -> None:
    fake = FakeSidecar("afk")
    original = fake.handle

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/slice-async" and request.method == "POST":
            fake.uploads.append([])
            return httpx.Response(202, json={"requestId": "../../evil", "statusUrl": "http://x/"})
        return original(request)

    module = OrcaSlicerApi(URL, transport=httpx.MockTransport(handle))
    with pytest.raises(ModuleError, match="request id"):
        module.slice(job(), Log())


def test_async_timeout() -> None:
    fake = FakeSidecar("afk", statuses=["processing"])
    with pytest.raises(ModuleError, match="didn't finish within"):
        api(fake, OrcaSlicerApi, timeout_s=0.05).slice(job(), Log())


def test_sync_reports_progress_while_slicing() -> None:
    fake = FakeSidecar("resolver", block=threading.Event())
    threading.Timer(0.3, fake.block.set).start()  # type: ignore[union-attr]
    log = Log()
    api(fake).slice(job(), log)
    assert "Slicing: Generating G-code (75%)" in log
    rid = fake.fields()["requestId"]
    assert f"GET /slice/progress/{rid}" in fake.paths


def test_sync_timeout() -> None:
    fake = FakeSidecar("resolver", block=threading.Event())
    try:
        with pytest.raises(ModuleError, match="didn't finish within"):
            api(fake, timeout_s=0.2).slice(job(), Log())
    finally:
        fake.block.set()  # type: ignore[union-attr]


# -- errors ------------------------------------------------------------------------------

CLI_FAILURE = {
    "message": "Slicing failed with error from slicer: The selected printer is not compatible"
    " with the process preset in the 3mf.",
    "details": "Slicer process failed (exit code 239)\nstdout: [2026-10-01 21:53:33.847272] "
    "[0x00007f17655cf540] [trace]   Initializing StaticPrintConfigs\n[2026-10-01 "
    "21:53:33.854697] [0x00007f17655cf540] [error]   run 3002: process not compatible with "
    "printer.\nrun found error, return -17, exit...",
}


def test_cli_failure_keeps_reason_and_error_lines() -> None:
    fake = FakeSidecar("resolver", slice_error=(500, CLI_FAILURE))
    with pytest.raises(ModuleError) as e:
        api(fake).slice(job(), Log())
    assert "HTTP 500" in e.value.message
    assert "not compatible with the process preset" in e.value.message
    assert "run 3002: process not compatible with printer." in e.value.message
    assert "Initializing StaticPrintConfigs" not in e.value.message


def test_bad_input_reason() -> None:
    body = {"message": "Invalid file type. Only STL and 3MF files are allowed."}
    fake = FakeSidecar("afk", slice_error=(400, body))
    with pytest.raises(ModuleError, match=r"HTTP 400.*Only STL and 3MF"):
        api(fake, OrcaSlicerApi).slice(job(), Log())


def test_unreachable() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    module = BambuStudioApi(URL, transport=httpx.MockTransport(refuse))
    with pytest.raises(
        ModuleError, match=re.escape("Can't reach the slicer at http://172.30.32.1:3001")
    ):
        module.slice(job(), Log())


def test_not_a_3mf_refused() -> None:
    fake = FakeSidecar("resolver", result=b"<html>proxy error</html>")
    with pytest.raises(ModuleError, match="isn't a 3MF"):
        api(fake).slice(job(), Log())
    fake = FakeSidecar("resolver", result=_zip_without_gcode())
    with pytest.raises(ModuleError, match="no G-code"):
        api(fake).slice(job(), Log())


def _zip_without_gcode() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Metadata/slice_info.config", "<config/>")
    return buf.getvalue()


# -- media and stats ---------------------------------------------------------------------


def test_media_gcode() -> None:
    fake = FakeSidecar("resolver", result_headers={})
    out = api(fake).slice(job(media="gcode"), Log())
    assert "exportType" not in fake.fields()
    assert out.media == "gcode" and out.filename == "Part 1 (JHD).gcode"
    assert out.data.startswith(b"; HEADER_BLOCK_START")
    assert (out.print_time_s, out.material_g, out.layers) == (15 * 60 + 5, 5.88, 32)


def test_media_gcode_but_zip_refused() -> None:
    fake = FakeSidecar("resolver", result=gcode_3mf())
    with pytest.raises(ModuleError, match="ZIP"):
        api(fake).slice(job(media="gcode"), Log())


def test_media_not_made_refused() -> None:
    with pytest.raises(ModuleError, match="bgcode"):
        api(FakeSidecar()).slice(job(media="bgcode"), Log())


def test_stats_fall_back_to_gcode_header_and_sum_filaments() -> None:
    # No slice_info values: the G-code header, which lists one weight per filament.
    fake = FakeSidecar("resolver", result=gcode_3mf(prediction=None, weight=None))
    out = api(fake).slice(job(), Log())
    assert out.print_time_s == 15 * 60 + 5
    assert out.material_g == pytest.approx(10.95)  # 5.64 + 5.31, not the header's 5.88
    fake = FakeSidecar("resolver", result=gcode(grams="0"), result_headers={})
    out = api(fake).slice(job(media="gcode"), Log())
    assert out.material_g is None


# -- live --------------------------------------------------------------------------------

LIVE_URL = os.environ.get("OS2SLICE_LIVE_SLICERAPI_URL", "")
SPIKE_STL = Path(__file__).resolve().parents[1] / "scripts" / "spike_JHD.stl"


def _pick(names: tuple[str, ...], *wanted: str) -> str:
    for word in wanted:
        hit = next((n for n in names if word in n), None)
        if hit:
            return hit
    assert names, "the slicer offers no profiles"
    return names[0]


@pytest.mark.live
def test_live_slice_spike_part() -> None:
    """Slice scripts/spike_JHD.stl on a real sidecar. Never prints: a slicer can't."""
    if not LIVE_URL:
        pytest.skip("OS2SLICE_LIVE_SLICERAPI_URL not set")
    if not SPIKE_STL.exists():
        pytest.skip(f"{SPIKE_STL} missing (it's gitignored; copy it from the spike)")
    module = BambuStudioApi(LIVE_URL, 600)
    model = os.environ.get("OS2SLICE_LIVE_SLICERAPI_MODEL", "A1 mini")
    catalog = module.profiles(model)
    printer = _pick(catalog.printer, "0.4 nozzle")
    tag = "@BBL A1M" if "A1 mini" in printer else ""
    profiles = Profiles(
        printer,
        _pick(catalog.process, f"0.20mm Standard {tag}".strip(), "0.20mm Standard"),
        _pick(catalog.filament, f"Bambu PLA Basic {tag}".strip(), "Generic PLA", "PLA"),
    )
    bed = (180.0, 180.0) if "A1 mini" in printer else (256.0, 256.0)
    printer_info = PrinterInfo("live", "live", "fdm", model, "t", "s", bed_mm=bed)
    for copies, media in ((1, "gcode.3mf"), (2, "gcode")):
        settings = PrintSettings(walls=3, infill=20, copies=copies)
        live_job = SliceInput(
            "spike JHD",
            printer_info,
            (PartGeometry("JHD", SPIKE_STL.read_bytes()),),
            settings,
            profiles,
            copies=copies,
            media=media,  # type: ignore[arg-type]
        )
        out = module.slice(live_job, lambda line: None)
        assert out.data and out.media == media
        assert out.layers and out.print_time_s and out.material_g
