"""End to end through the core: plan_print → execute_print with every module pairing.

Each config names real module kinds; `Modules.from_config(..., transports={key: ...})`
gives each module its own fake service, and the part comes from the fake Onshape. No
module is called directly: what's asserted is what the core makes them do.
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from os2slice import config, printing
from os2slice.auth import Keys
from os2slice.errors import ConfigError
from os2slice.modules.base import ModuleAuthError
from os2slice.modules.registry import Modules
from os2slice.modules.slicerapi import SlicerApi
from os2slice.onshape import OnshapeClient
from os2slice.orientation import Orientation
from os2slice.request import parse_onshape_url
from os2slice.settings import PrintSettings
from tests.conftest import DOC, ELEM, WS
from tests.fakes import FakeBambuddy, fake_onshape
from tests.fakes_printers import MOONRAKER_URL, PRUSALINK_URL, FakeMoonraker, FakePrusaLink
from tests.fakes_slicerapi import A1M, FakeSidecar

URL = f"https://cad.onshape.com/documents/{DOC}/w/{WS}/e/{ELEM}"
A1_PROFILES = {
    "printer": A1M,
    "process": "0.20mm Standard @BBL A1M",
    "filament": "Bambu PLA Basic @BBL A1M",
}
VORON_PROFILES = {
    "printer": "Voron 2.4 350 0.4 nozzle",
    "process": "0.20mm Standard @Voron",
    "filament": "Generic PLA @System",
}
MK4_PROFILES = {
    "printer": "Prusa MK4 0.4 nozzle",
    "process": "0.20mm SPEED @MK4",
    "filament": "Prusament PLA",
}


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(SlicerApi, "poll_s", 0.01)


@pytest.fixture
def onshape() -> OnshapeClient:
    return OnshapeClient(
        "https://cad.onshape.com", Keys("a", "b"), transport=httpx.MockTransport(fake_onshape)
    )


def parse(tmp_path: Path, **tables: Any) -> config.Config:
    return config.parse({"export": {"dir": str(tmp_path / "exports")}, **tables}, tmp_path / "c")


def secrets(values: dict[str, str]):  # type: ignore[no-untyped-def]
    return lambda name: values.get(name)


def run(
    cfg: config.Config,
    onshape: OnshapeClient,
    modules: Modules,
    printer: str,
    settings: PrintSettings | None = None,
) -> tuple[printing.PrintPlan, printing.PrintOutcome]:
    req = parse_onshape_url(URL, "e2e", ["e2e"], "JHD")
    plan = printing.plan_print(
        req,
        cfg,
        onshape,
        modules,
        printer,
        Orientation.parse("as-modeled"),
        settings or PrintSettings(3, 25),
    )
    steps: list[str] = []
    outcome = printing.execute_print(plan, cfg, onshape, modules, queue=True, progress=steps.append)
    assert steps[0] == "Working out the orientation"
    assert f"Queueing on {plan.printer.name}" in steps
    return plan, outcome


# -- (a) Bambu Studio sidecar slices, BamBuddy queues -----------------------------------


def test_studio_sidecar_slices_and_bambuddy_queues_two_copies(
    tmp_path: Path, onshape: OnshapeClient
) -> None:
    cfg = parse(
        tmp_path,
        default_printer="A1 Mini",
        slicers={"studio": {"kind": "bambu-studio-api", "url": "http://studio.test:3001"}},
        targets={
            "farm": {
                "kind": "bambuddy",
                "url": "http://bambuddy.test:8000",
                "models": {"A1 Mini": {"slicer": "studio", "profiles": A1_PROFILES}},
            }
        },
    )
    bb, studio = FakeBambuddy(), FakeSidecar("resolver")
    with Modules.from_config(
        cfg,
        secrets=secrets({"targets.farm.api_key": "bb-key"}),
        transports={"farm": httpx.MockTransport(bb), "studio": studio.transport()},
    ) as modules:
        plan, out = run(cfg, onshape, modules, "A1 Mini", PrintSettings(3, 25, copies=2))
    assert plan.printer.key == "farm/1" and plan.printer.slicer == "studio"
    assert plan.manual_start and plan.target_label == "BamBuddy"
    # Sliced by the sidecar, as a project with one build item per copy.
    assert studio.fields()["exportType"] == "3mf"
    (model,) = studio.files(name="file")
    with zipfile.ZipFile(io.BytesIO(model.data)) as z:
        assert len(re.findall(r"<item ", z.read("3D/3dmodel.model").decode())) == 2
    assert "authorization" not in studio.requests[-1].headers  # no slicer key configured
    assert out.slice.media == "gcode.3mf" and out.slice.report["slicer"] == "bambu-studio-api"
    # BamBuddy never sliced; it got the sidecar's file in its library and queued it.
    assert bb.slice_bodies == []
    (upload,) = bb.uploads
    head = upload[: upload.index(b"PK")].decode(errors="replace")
    assert re.search(r'filename="[^"]+\.gcode\.3mf"', head)
    assert out.slice.data in upload
    assert [f["name"] for f in bb.folders] == ["Onshape"]
    assert bb.queued == [{"library_file_id": 30, "printer_id": 1, "manual_start": True}]
    assert out.submission is not None and out.submission.state == "waiting"


# -- (b) OrcaSlicer sidecar → Moonraker -------------------------------------------------


def voron_config(tmp_path: Path, **printer: Any) -> config.Config:
    return parse(
        tmp_path,
        default_printer="voron",
        slicers={"orca-api": {"kind": "orca-slicer-api", "url": "http://orca.test:3003"}},
        targets={"voron": {"kind": "moonraker", "url": MOONRAKER_URL}},
        printers={
            "voron": {
                "target": "voron",
                "slicer": "orca-api",
                "model": "Voron 2.4 350",
                "bed_mm": [350, 350],
                "profiles": VORON_PROFILES,
                "materials": ["PLA black", "PETG grey"],
                **printer,
            }
        },
    )


def test_orca_sidecar_to_moonraker_uploads_gcode_and_waits(
    tmp_path: Path, onshape: OnshapeClient
) -> None:
    cfg = voron_config(tmp_path)
    voron, orca = FakeMoonraker(), FakeSidecar("afk")
    with Modules.from_config(
        cfg,
        secrets=secrets({}),
        transports={"voron": httpx.MockTransport(voron), "orca-api": orca.transport()},
    ) as modules:
        plan, out = run(cfg, onshape, modules, "voron")
        # Moonraker can't tell what's loaded: the menu falls back to the configured list.
        assert [m.label for m in printing.materials_of(plan.printer, plan.status)] == [
            "PLA black",
            "PETG grey",
        ]
    assert plan.printer.bed_mm == (350.0, 350.0) and plan.status.ready
    assert plan.manual_start and plan.target_label == "Moonraker (Klipper)"
    assert "exportType" not in orca.fields()  # plain G-code, the one medium Moonraker takes
    (model,) = orca.files(name="file")
    assert model.filename.endswith(".stl")
    assert out.slice.media == "gcode" and out.slice.filename.endswith(".gcode")
    (upload,) = voron.uploads
    assert "print" not in upload  # never started by os2slice (D-13)
    assert upload["root"] == "gcodes" and upload["path"] == "os2slice"
    assert upload["file_name"].endswith(".gcode") and upload["file"] == out.slice.data
    assert out.submission is not None and out.submission.state == "waiting"
    assert "start it from Mainsail" in out.submission.detail


# -- (c) OrcaSlicer sidecar → PrusaLink -------------------------------------------------


def test_orca_sidecar_to_prusalink_puts_without_printing(
    tmp_path: Path, onshape: OnshapeClient
) -> None:
    cfg = parse(
        tmp_path,
        slicers={"orca-api": {"kind": "orca-slicer-api", "url": "http://orca.test:3003"}},
        targets={"mk4": {"kind": "prusalink", "url": PRUSALINK_URL}},
        printers={
            "MK4": {"target": "mk4", "slicer": "orca-api", "model": "MK4", "profiles": MK4_PROFILES}
        },
    )
    mk4, orca = FakePrusaLink(), FakeSidecar("afk")
    with Modules.from_config(
        cfg,
        secrets=secrets({"targets.mk4.api_key": "secret-key"}),
        transports={"mk4": httpx.MockTransport(mk4), "orca-api": orca.transport()},
    ) as modules:
        plan, out = run(cfg, onshape, modules, "MK4")
    assert plan.printer.extra["nozzle_diameter"] == 0.4  # filled in from the printer
    assert out.slice.media == "gcode"
    (put,) = mk4.uploads
    assert put.method == "PUT" and put.url.path.startswith("/api/v1/files/usb/os2slice/")
    assert put.url.path.endswith(".gcode") and put.content == out.slice.data
    assert put.headers["Print-After-Upload"] == "?0" and put.headers["Overwrite"] == "?0"
    assert out.submission is not None and out.submission.state == "waiting"


# -- (d) pairings refused at load ----------------------------------------------------


def test_incompatible_media_refused_at_load(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"BamBuddy makes gcode\.3mf, but Moonraker") as e:
        parse(
            tmp_path,
            targets={
                "farm": {"kind": "bambuddy", "url": "http://bambuddy.test:8000"},
                "voron": {"kind": "moonraker", "url": MOONRAKER_URL},
            },
            printers={"voron": {"target": "voron", "slicer": "farm"}},
        )
    assert 'printers."voron"' in e.value.message and "takes gcode" in e.value.message
    with pytest.raises(ConfigError, match=r"needs a slicer \(Moonraker \(Klipper\) doesn't slice"):
        parse(
            tmp_path,
            targets={"voron": {"kind": "moonraker", "url": MOONRAKER_URL}},
            printers={"voron": {"target": "voron"}},
        )
    with pytest.raises(ConfigError, match="orca-slicer-api isn't a target"):
        parse(tmp_path, targets={"x": {"kind": "orca-slicer-api", "url": "http://o.test"}})


def test_compatible_pairs_load(tmp_path: Path) -> None:
    cfg = parse(
        tmp_path,
        slicers={
            "orca-api": {"kind": "orca-slicer-api", "url": "http://orca.test:3003"},
            "studio": {"kind": "bambu-studio-api", "url": "http://studio.test:3001"},
        },
        targets={
            "farm": {
                "kind": "bambuddy",
                "url": "http://bambuddy.test:8000",
                "models": {"X1C": {"slicer": "studio"}, "A1": {"slicer": "orca-api"}},
            },
            "voron": {"kind": "moonraker", "url": MOONRAKER_URL},
            "mk4": {"kind": "prusalink", "url": PRUSALINK_URL},
        },
        printers={
            "voron": {"target": "voron", "slicer": "studio"},
            "MK4": {"target": "mk4", "slicer": "orca-api"},
        },
    )
    sk = {k: m.kind for k, m in cfg.slicer_modules.items()}
    tk = {k: m.kind for k, m in cfg.targets.items()}
    assert sk == {"orca-api": "orca-slicer-api", "studio": "bambu-studio-api"}
    assert tk == {"farm": "bambuddy", "voron": "moonraker", "mk4": "prusalink"}
    s, t = cfg.slicer_modules["orca-api"], cfg.targets["mk4"]
    assert s.values["timeout_s"] == 900 and t.values["storage"] == "usb"


# -- (e) a refused key surfaces as ModuleAuthError ------------------------------------


def test_target_refusing_the_key_is_a_module_auth_error(
    tmp_path: Path, onshape: OnshapeClient
) -> None:
    cfg = voron_config(tmp_path)
    voron, orca = FakeMoonraker(), FakeSidecar("afk")
    modules = Modules.from_config(
        cfg,
        secrets=secrets({}),
        transports={"voron": httpx.MockTransport(voron), "orca-api": orca.transport()},
    )
    req = parse_onshape_url(URL, "e2e", ["e2e"], "JHD")
    plan = printing.plan_print(
        req, cfg, onshape, modules, None, Orientation.parse("as-modeled"), PrintSettings()
    )
    voron.api_key = "now-required"  # Moonraker starts refusing between plan and print
    with pytest.raises(ModuleAuthError, match=r"refused the request \(401") as e:
        printing.execute_print(plan, cfg, onshape, modules, queue=True)
    assert e.value.http_status == 502 and e.value.exit_code == 3
    assert "[targets.voron]" in e.value.fix
    assert voron.uploads == [] and len(orca.uploads) == 1  # sliced, then refused
    modules.close()


def test_slicer_proxy_refusing_the_key_is_a_module_auth_error(
    tmp_path: Path, onshape: OnshapeClient
) -> None:
    cfg = voron_config(tmp_path)
    voron = FakeMoonraker()
    orca = FakeSidecar("afk", slice_error=(401, {"message": "Unauthorized"}))
    with Modules.from_config(
        cfg,
        secrets=secrets({"slicers.orca-api.api_key": "proxy-key"}),
        transports={"voron": httpx.MockTransport(voron), "orca-api": orca.transport()},
    ) as modules:
        req = parse_onshape_url(URL, "e2e", ["e2e"], "JHD")
        plan = printing.plan_print(
            req, cfg, onshape, modules, None, Orientation.parse("as-modeled"), PrintSettings()
        )
        with pytest.raises(ModuleAuthError, match="HTTP 401") as e:
            printing.execute_print(plan, cfg, onshape, modules, queue=True)
    assert "[slicers.orca-api]" in e.value.fix
    assert orca.requests[-1].headers["authorization"] == "Bearer proxy-key"
    assert voron.uploads == []


def test_unreachable_printer_plans_as_offline(tmp_path: Path, onshape: OnshapeClient) -> None:
    cfg = voron_config(tmp_path)

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    with Modules.from_config(
        cfg,
        secrets=secrets({}),
        transports={"voron": httpx.MockTransport(down), "orca-api": FakeSidecar().transport()},
    ) as modules:
        req = parse_onshape_url(URL, "e2e", ["e2e"], "JHD")
        plan = printing.plan_print(
            req, cfg, onshape, modules, None, Orientation.parse("as-modeled"), PrintSettings()
        )
    assert (plan.status.state, plan.status.connected, plan.status.ready) == (
        "offline",
        False,
        False,
    )


# -- doctor ---------------------------------------------------------------------------

DOCTOR_TOML = f"""
[slicers.studio]
kind = "bambu-studio-api"
url = "http://studio.test:3001"

[slicers.orca-api]
kind = "orca-slicer-api"
url = "http://orca.test:3003"

[targets.voron]
kind = "moonraker"
url = "{MOONRAKER_URL}"

[targets.mk4]
kind = "prusalink"
url = "{PRUSALINK_URL}"

[printers.voron]
target = "voron"
slicer = "orca-api"
profiles = {{ printer = "Voron", process = "0.20mm", filament = "PLA" }}

[printers.MK4]
target = "mk4"
slicer = "studio"
"""


def test_doctor_renders_a_row_per_new_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import functools

    from os2slice import cli
    from os2slice.config import config_path

    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'[export]\ndir = "{tmp_path / "exports"}"\n' + DOCTOR_TOML)
    monkeypatch.setenv("OS2SLICE_SECRET_TARGETS_MK4_API_KEY", "wrong-key")
    real = Modules.from_config.__func__  # type: ignore[attr-defined]
    transports = {
        "studio": FakeSidecar("resolver").transport(),  # the add-on's real "unhealthy" 503
        "orca-api": FakeSidecar("afk").transport(),
        "voron": httpx.MockTransport(FakeMoonraker()),
        "mk4": httpx.MockTransport(FakePrusaLink()),  # refuses "wrong-key"
    }
    monkeypatch.setattr(
        Modules, "from_config", classmethod(functools.partial(real, transports=transports))
    )
    assert cli.main(["doctor"]) == 1  # the refused PrusaLink key (and no Onshape keys)
    rows = capsys.readouterr().out.splitlines()

    def row(status: str, check: str) -> str:
        hits = [r for r in rows if re.match(rf"{status}  {re.escape(check)}\s", r)]
        assert hits, f"no {status} row for {check!r} in:\n" + "\n".join(rows)
        return hits[0]

    assert "Moonraker v0.9.3-12-gabcdef0, Klipper v0.12.0" in row("PASS", "target voron")
    assert "PrusaLink refused the API key" in row("FAIL", "target mk4")
    assert "targets.mk4.api_key" in row("FAIL", "target mk4")
    assert "OrcaSlicer API sidecar healthy, slicer 2.4.2" in row("PASS", "slicer orca-api")
    assert "Bambu Studio API sidecar healthy" in row("PASS", "slicer studio")
    assert "dataPath" in row("WARN", "slicer studio")
    assert not any(re.match(r"WARN  (target voron|target mk4|slicer orca-api)\s", r) for r in rows)
    assert "wrong-key" not in "\n".join(rows)
