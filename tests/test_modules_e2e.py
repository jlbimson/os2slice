"""End to end through the core: plan_print → execute_print with every module pairing.

Each config names real module kinds; `Modules.from_config(..., transports={key: ...})`
gives each module its own fake service, and the part comes from the fake Onshape. No
module is called directly: what's asserted is what the core makes them do.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from os2slice import config, printing
from os2slice.auth import Keys
from os2slice.errors import BadRequest, ConfigError
from os2slice.modules.base import ModuleAuthError, ModuleError
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


# -- (f) a sidecar with the user's own GUI profiles (profile_dir) -----------------------


def own_profiles_config(tmp_path: Path) -> config.Config:
    """A Klipper printer whose slicer reads an OrcaSlicer config folder, no materials."""
    root = tmp_path / "OrcaSlicer"
    user = root / "user" / "0d1e5a7c"
    for kind, name, body in (
        ("machine", "JoshPrint 0.5 MMU", {"nozzle_diameter": ["0.5"]}),
        ("process", "0.2 Strong", {"wall_loops": "4"}),
        ("process", "0.2 Solid", {"wall_loops": "6"}),
        (
            "filament",
            "PM ASA",
            {"nozzle_temperature": ["260"], "default_filament_colour": ["#241F31"]},
        ),
        ("filament", "Sirayatech PET-CF", {"nozzle_temperature": ["300"]}),
    ):
        (user / kind).mkdir(parents=True, exist_ok=True)
        (user / kind / f"{name}.json").write_text(json.dumps({"name": name, **body}))
    return parse(
        tmp_path,
        default_printer="joshprint",
        slicers={
            "orca": {
                "kind": "orca-slicer-api",
                "url": "http://orca.test:3003",
                "profile_dir": str(root),
            }
        },
        targets={"joshprint": {"kind": "moonraker", "url": MOONRAKER_URL}},
        printers={
            "joshprint": {
                "target": "joshprint",
                "slicer": "orca",
                "model": "RatRig V-Core 3 300",
                "bed_mm": [300, 300],
                "profiles": {
                    "printer": "JoshPrint 0.5 MMU",
                    "process": "0.2 Strong",
                    "filament": "PM ASA",
                },
            }
        },
    )


def plan_own(
    cfg: config.Config, onshape: OnshapeClient, modules: Modules, **kw: Any
) -> printing.PrintPlan:
    req = parse_onshape_url(URL, "e2e", ["e2e"], "JHD")
    return printing.plan_print(
        req, cfg, onshape, modules, "joshprint", Orientation.parse("as-modeled"),
        PrintSettings(3, 25), **kw,
    )  # fmt: skip


def test_own_filaments_are_the_choices_and_a_process_can_be_picked(
    tmp_path: Path, onshape: OnshapeClient
) -> None:
    cfg = own_profiles_config(tmp_path)
    klipper, orca = FakeMoonraker(), FakeSidecar("resolver")
    with Modules.from_config(
        cfg,
        secrets=secrets({}),
        transports={"joshprint": httpx.MockTransport(klipper), "orca": orca.transport()},
    ) as modules:
        chosen = modules.find("joshprint")
        own = modules.own_profiles(chosen)
        assert own.process == ("0.2 Solid", "0.2 Strong")
        menu = printing.materials_of(chosen, modules.target_for(chosen).status(chosen), own)
        assert [m.label for m in menu] == ["PM ASA", "Sirayatech PET-CF"]
        petcf = menu[1]
        assert re.fullmatch(r"f-[0-9a-f]{12}", petcf.id) and petcf.profile == "Sirayatech PET-CF"

        plan = plan_own(cfg, onshape, modules, material=petcf.id, process="0.2 Solid")
        assert plan.profiles.process == "0.2 Solid"
        assert plan.profiles.filament == "Sirayatech PET-CF"
        printing.execute_print(plan, cfg, onshape, modules, queue=True)

        # Several parts: each its own filament profile, as two filaments in one print.
        plan = plan_own(cfg, onshape, modules, material=menu[0].id, extra_parts=[("JKD", petcf.id)])
        printing.execute_print(plan, cfg, onshape, modules, queue=True)

    first, second = orca.uploads[0], orca.uploads[-1]
    process = next(p for p in first if p.name == "presetProfile").json()
    assert process["name"] == "0.2 Solid" and process["wall_loops"] == "3"  # the panel's walls
    (filament,) = [p.json() for p in first if p.name == "filamentProfile"]
    assert filament["nozzle_temperature"] == ["300"]
    assert filament["compatible_printers"] == ["JoshPrint 0.5 MMU"]
    filaments = [p.json() for p in second if p.name == "filamentProfile"]
    assert [f["name"] for f in filaments] == ["PM ASA", "Sirayatech PET-CF"]
    # Every filament has a colour, or the OrcaSlicer CLI crashes: its profile's, or a stand-in.
    assert [f["filament_colour"] for f in filaments] == [["#241F31"], ["#E01B24"]]
    assert "filament_colour" not in filament  # one filament: left to the profile
    assert len(klipper.uploads) == 2


def test_a_process_that_isnt_ours_is_refused(tmp_path: Path, onshape: OnshapeClient) -> None:
    cfg = own_profiles_config(tmp_path)
    with Modules.from_config(
        cfg,
        secrets=secrets({}),
        transports={
            "joshprint": httpx.MockTransport(FakeMoonraker()),
            "orca": FakeSidecar("resolver").transport(),
        },
    ) as modules:
        with pytest.raises(BadRequest, match=r"No process profile '0\.20mm Standard @RatRig'"):
            plan_own(cfg, onshape, modules, process="0.20mm Standard @RatRig")
        # The configured one is always allowed, own or not.
        assert (
            plan_own(cfg, onshape, modules, process="0.2 Strong").profiles.process == "0.2 Strong"
        )


# -- (g) the panel's extra settings ([panel] extra_settings) -----------------------------


def test_extra_settings_reach_the_sidecars_process_and_every_filament(
    tmp_path: Path, onshape: OnshapeClient
) -> None:
    cfg = own_profiles_config(tmp_path)
    klipper, orca = FakeMoonraker(), FakeSidecar("resolver")
    extras = (("chamber_temperature", "55"), ("bed_temperature", "100"), ("layer_height", "0.28"))
    with Modules.from_config(
        cfg,
        secrets=secrets({}),
        transports={"joshprint": httpx.MockTransport(klipper), "orca": orca.transport()},
    ) as modules:
        chosen = modules.find("joshprint")
        menu = printing.materials_of(
            chosen, modules.target_for(chosen).status(chosen), modules.own_profiles(chosen)
        )
        req = parse_onshape_url(URL, "e2e", ["e2e"], "JHD")
        plan = printing.plan_print(
            req, cfg, onshape, modules, "joshprint", Orientation.parse("as-modeled"),
            PrintSettings(3, 25, extras=extras), menu[0].id, "High Temp Plate",
            [("JKD", menu[1].id)],
        )  # fmt: skip
        printing.execute_print(plan, cfg, onshape, modules, queue=True)
    upload = orca.uploads[-1]
    process = next(p for p in upload if p.name == "presetProfile").json()
    assert process["layer_height"] == "0.28" and "chamber_temperature" not in process
    filaments = [p.json() for p in upload if p.name == "filamentProfile"]
    assert len(filaments) == 2
    for f in filaments:
        assert f["chamber_temperature"] == ["55"]
        assert f["hot_plate_temp"] == ["100"] and f["hot_plate_temp_initial_layer"] == ["100"]
    assert "chamber temperature 55 °C" in plan.settings.describe()


def test_bambuddy_refuses_filament_extras_but_takes_process_ones(
    tmp_path: Path, onshape: OnshapeClient
) -> None:
    cfg = parse(
        tmp_path,
        default_printer="A1 Mini",
        targets={
            "farm": {
                "kind": "bambuddy",
                "url": "http://bambuddy.test:8000",
                "models": {"A1 Mini": {"profiles": A1_PROFILES}},
            }
        },
    )
    bb = FakeBambuddy(job_states=["completed"])
    with Modules.from_config(
        cfg,
        secrets=secrets({"targets.farm.api_key": "bb-key"}),
        transports={"farm": httpx.MockTransport(bb)},
    ) as modules:
        with pytest.raises(ModuleError, match="BamBuddy slices with its filament presets"):
            run(
                cfg,
                onshape,
                modules,
                "A1 Mini",
                PrintSettings(extras=(("chamber_temperature", "50"),)),
            )
        assert bb.uploads == []  # refused before anything went up
        run(cfg, onshape, modules, "A1 Mini", PrintSettings(extras=(("layer_height", "0.16"),)))
    assert bb.slice_bodies[-1]["process_overrides"]["layer_height"] == "0.16"


# -- (h) a filament changer's tools (Happy Hare on Moonraker) ------------------------------


def mmu_config(tmp_path: Path) -> config.Config:
    """joshprint: an MMU and a plain printer profile, filaments with their types."""
    root = tmp_path / "OrcaSlicer"
    user = root / "user" / "0d1e5a7c"
    mmu_start = "MMU_START_SETUP INITIAL_TOOL={initial_tool}\nPRINT_WARMUP"
    for kind, name, body in (
        ("machine", "JoshPrint 0.5 MMU", {"machine_start_gcode": mmu_start}),
        ("machine", "JoshPrint 0.5", {"machine_start_gcode": "PRINT_WARMUP\nSTART_PRINT"}),
        ("process", "0.2 Strong", {"wall_loops": "4"}),
        ("filament", "PM ASA", {"filament_type": ["ASA"], "default_filament_colour": ["#F2754E"]}),
        ("filament", "3DO ASA", {"filament_type": ["ASA"]}),
        ("filament", "3DO PETG", {"filament_type": ["PETG"]}),
        ("filament", "Creality PETG", {"filament_type": ["PETG"]}),
    ):
        (user / kind).mkdir(parents=True, exist_ok=True)
        (user / kind / f"{name}.json").write_text(json.dumps({"name": name, **body}))
    return parse(
        tmp_path,
        default_printer="joshprint",
        slicers={
            "orca": {
                "kind": "orca-slicer-api",
                "url": "http://orca.test:3003",
                "profile_dir": str(root),
            }
        },
        targets={"joshprint": {"kind": "moonraker", "url": MOONRAKER_URL}},
        printers={
            "joshprint": {
                "target": "joshprint",
                "slicer": "orca",
                "model": "RatRig V-Core 3 300",
                "bed_mm": [300, 300],
                "profiles": {
                    "printer": "JoshPrint 0.5 MMU",
                    "process": "0.2 Strong",
                    "filament": "PM ASA",
                },
            }
        },
    )


def test_changer_tools_are_matched_and_all_loaded_in_tool_order(
    tmp_path: Path, onshape: OnshapeClient
) -> None:
    from tests.fakes_printers import HAPPY_HARE, HAPPY_HARE_SPOOLS

    cfg = mmu_config(tmp_path)
    klipper, orca = FakeMoonraker(mmu=HAPPY_HARE, spools=HAPPY_HARE_SPOOLS), FakeSidecar("resolver")
    with Modules.from_config(
        cfg,
        secrets=secrets({}),
        transports={"joshprint": httpx.MockTransport(klipper), "orca": orca.transport()},
    ) as modules:
        chosen = modules.find("joshprint")
        own = modules.own_profiles(chosen)
        assert own.mmu_printers == ("JoshPrint 0.5 MMU",)
        assert own.filament_types["Creality PETG"] == "PETG"
        menu = printing.materials_of(chosen, modules.target_for(chosen).status(chosen), own)
        tools = [m for m in menu if printing.is_tool(m)]
        # Vendor first (Creality, 3DO), then the printer's own filament; an empty gate has none.
        assert [m.profile for m in tools] == ["PM ASA", "3DO PETG", "Creality PETG", "PM ASA", ""]
        assert [m.label for m in menu[5:]] == ["3DO ASA", "3DO PETG", "Creality PETG", "PM ASA"]

        # One part on T2: every tool goes up, in order, and the part is filament 3.
        plan = plan_own(cfg, onshape, modules, material="t2")
        assert [m.id for m in plan.tools] == ["t0", "t1", "t2", "t3", "t4"]
        assert plan.tools[4].profile == "PM ASA"  # the empty gate: a stand-in profile
        printing.execute_print(plan, cfg, onshape, modules, queue=True)

        with pytest.raises(BadRequest, match="doesn't use the filament changer"):
            plan_own(cfg, onshape, modules, material="t0", machine="JoshPrint 0.5")
        with pytest.raises(BadRequest, match="Pick changer tools for every part"):
            plan_own(cfg, onshape, modules, material="t0", extra_parts=[("JKD", menu[5].id)])
        # A filament profile instead: one filament, as before, with the plain printer.
        plan = plan_own(cfg, onshape, modules, material=menu[6].id, machine="JoshPrint 0.5")
        assert plan.tools == () and plan.profiles.printer == "JoshPrint 0.5"

    upload = orca.uploads[-1]
    names = [p.json()["name"] for p in upload if p.name == "filamentProfile"]
    assert names == ["PM ASA", "3DO PETG", "Creality PETG", "PM ASA", "PM ASA"]
    colours = [p.json()["filament_colour"] for p in upload if p.name == "filamentProfile"]
    assert colours == [["#000000"], ["#000000"], ["#00FFFF"], ["#FFFFFF"], ["#FF8400"]]
    (model,) = [p for p in upload if p.name == "file"]
    assert model.filename.endswith(".3mf")  # one part, still a project: it names its tool
    with zipfile.ZipFile(io.BytesIO(model.data)) as z:
        settings = z.read("Metadata/model_settings.config").decode()
    assert re.findall(r'key="extruder" value="(\d+)"', settings) == ["3", "3"]


def test_a_filament_that_isnt_loaded_goes_into_a_chosen_tool(
    tmp_path: Path, onshape: OnshapeClient
) -> None:
    from tests.fakes_printers import HAPPY_HARE, HAPPY_HARE_SPOOLS

    cfg = mmu_config(tmp_path)
    klipper, orca = FakeMoonraker(mmu=HAPPY_HARE, spools=HAPPY_HARE_SPOOLS), FakeSidecar("resolver")
    with Modules.from_config(
        cfg,
        secrets=secrets({}),
        transports={"joshprint": httpx.MockTransport(klipper), "orca": orca.transport()},
    ) as modules:
        chosen = modules.find("joshprint")
        menu = printing.materials_of(
            chosen, modules.target_for(chosen).status(chosen), modules.own_profiles(chosen)
        )
        by_name = {m.label: m.id for m in menu if not printing.is_tool(m)}
        # 3DO ASA is on no gate: put it in T1 (now PolyLite Blue).
        plan = plan_own(cfg, onshape, modules, material=f"t1.{by_name['3DO ASA']}")
        assert [m.profile for m in plan.tools] == [
            "PM ASA", "3DO ASA", "Creality PETG", "PM ASA", "PM ASA"
        ]  # fmt: skip
        assert plan.tools[1].label == "T1: 3DO ASA (load it first)"
        assert plan.to_load() == ["T1: load 3DO ASA (now T1: PolyLite™ ASA Blue (PETG))"]
        assert "Before start: T1: load 3DO ASA" in "\n".join(plan.summary_lines())
        printing.execute_print(plan, cfg, onshape, modules, queue=True)
        # A filament already in that tool needs no loading.
        plan = plan_own(cfg, onshape, modules, material=f"t2.{by_name['Creality PETG']}")
        assert plan.to_load() == [] and plan.material is not None and plan.material.id == "t2"
        # Two parts can't claim one tool with different filaments.
        with pytest.raises(BadRequest, match="T2 can't hold both"):
            plan_own(cfg, onshape, modules, material="t2",
                     extra_parts=[("JKD", f"t2.{by_name['3DO ASA']}")])  # fmt: skip
        with pytest.raises(BadRequest, match="has no tool T9"):
            plan_own(cfg, onshape, modules, material=f"t9.{by_name['3DO ASA']}")
    names = [p.json()["name"] for p in orca.uploads[0] if p.name == "filamentProfile"]
    assert names == ["PM ASA", "3DO ASA", "Creality PETG", "PM ASA", "PM ASA"]
    (model,) = [p for p in orca.uploads[0] if p.name == "file"]
    with zipfile.ZipFile(io.BytesIO(model.data)) as z:
        settings = z.read("Metadata/model_settings.config").decode()
    assert re.findall(r'key="extruder" value="(\d+)"', settings) == ["2", "2"]  # T1
