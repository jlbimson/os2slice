from __future__ import annotations

import dataclasses
from typing import Any, ClassVar

import httpx
import pytest

from os2slice import printing
from os2slice.auth import Keys
from os2slice.config import Config
from os2slice.errors import BadRequest, ConfigError
from os2slice.modules import bambu_project
from os2slice.modules.base import (
    Field,
    Health,
    ModelDefaults,
    ModuleSpec,
    ProfileCatalog,
    Profiles,
    SliceOutput,
)
from os2slice.modules.registry import Modules
from os2slice.onshape import OnshapeClient
from os2slice.orientation import Orientation
from os2slice.request import parse_onshape_url
from os2slice.settings import PrintSettings
from tests.conftest import DOC, ELEM, WS
from tests.fakes import FakeBambuddy, fake_onshape, uploaded_zip

URL = f"https://cad.onshape.com/documents/{DOC}/w/{WS}/e/{ELEM}"


@pytest.fixture
def onshape() -> OnshapeClient:
    return OnshapeClient(
        "https://cad.onshape.com", Keys("a", "b"), transport=httpx.MockTransport(fake_onshape)
    )


def mods(cfg: Config, fake) -> Modules:  # type: ignore[no-untyped-def]
    """The config's modules, talking to the fake (a FakeBambuddy or any handler)."""
    return Modules.from_config(cfg, secrets=lambda name: "k", transport=httpx.MockTransport(fake))


def with_models(cfg: Config, **models: ModelDefaults) -> Config:
    """`cfg` with more per-model defaults on its BamBuddy target."""
    t = cfg.targets["bambuddy"]
    t = dataclasses.replace(t, models={**t.models, **models})
    return dataclasses.replace(cfg, targets={**cfg.targets, "bambuddy": t})


def req(part: str | None = "JHD"):
    return parse_onshape_url(URL, "bambuddy", ["bambuddy"], part)


def plan(cfg, onshape, fake, orientation="as-modeled", printer=None):
    return printing.plan_print(
        req(),
        cfg,
        onshape,
        mods(cfg, fake),
        printer,
        Orientation.parse(orientation),
        PrintSettings(3, 25, "tree"),
    )


def test_plan_reads_only(cfg: Config, onshape: OnshapeClient) -> None:
    fake = FakeBambuddy(job_states=["completed"])
    p = plan(cfg, onshape, fake)
    assert (p.part_name, p.document_name, p.printer.name, p.status.describe()) == (
        "Part 1",
        "test",
        "A1 Mini",
        "IDLE",
    )
    assert p.profiles.process == "0.20mm Standard @BBL A1M"
    assert all(r.method == "GET" for r in fake.requests)
    assert "3 walls, 25% infill, tree supports" in "\n".join(p.summary_lines())


def test_plan_errors(cfg: Config, onshape: OnshapeClient) -> None:
    with pytest.raises(BadRequest, match="No active printer"):
        plan(cfg, onshape, FakeBambuddy(job_states=["completed"]), printer="Old")
    with pytest.raises(ConfigError, match="X1C"):
        plan(cfg, onshape, FakeBambuddy(job_states=["completed"]), printer="X1C_01")
    with pytest.raises(ConfigError, match="No printers are configured"):
        plan(dataclasses.replace(cfg, targets={}), onshape, FakeBambuddy(job_states=["completed"]))


def test_execute_queues_with_manual_start(cfg: Config, onshape: OnshapeClient) -> None:
    fake = FakeBambuddy(job_states=["completed"])
    p = plan(cfg, onshape, fake)
    steps: list[str] = []
    out = printing.execute_print(
        p, cfg, onshape, mods(cfg, fake), queue=True, progress=steps.append
    )
    assert out.queued and out.slice.report["library_file_id"] == 31
    assert out.submission is not None and out.submission.state == "waiting"
    assert fake.queued == [{"library_file_id": 31, "printer_id": 1, "manual_start": True}]
    assert [f["name"] for f in fake.folders] == ["Onshape", "test"]
    assert fake.slice_bodies[0]["auto_orient"] is False
    assert steps[0] == "Working out the orientation" and steps[-1] == "Queueing on A1 Mini"


def test_slice_only_never_queues(cfg: Config, onshape: OnshapeClient) -> None:
    fake = FakeBambuddy(job_states=["completed"])
    out = printing.execute_print(
        plan(cfg, onshape, fake), cfg, onshape, mods(cfg, fake), queue=False
    )
    assert not out.queued and fake.queued == []
    assert not any(r.url.path == "/api/v1/queue/" for r in fake.requests)


def test_auto_orient_and_face(cfg: Config, onshape: OnshapeClient) -> None:
    fake = FakeBambuddy(job_states=["completed"])
    printing.execute_print(
        plan(cfg, onshape, fake, "auto"), cfg, onshape, mods(cfg, fake), queue=False
    )
    assert fake.slice_bodies[-1]["auto_orient"] is True
    printing.execute_print(
        plan(cfg, onshape, fake, "face:JHO"), cfg, onshape, mods(cfg, fake), queue=False
    )
    assert fake.slice_bodies[-1]["auto_orient"] is False


def test_face_normal_lookup(onshape: OnshapeClient) -> None:
    assert onshape.face_normal(req(), "JHG") == (0.0, 0.0, -1.0)
    assert onshape.face_normal(req(), "JHO") == (-0.0, -1.0, -0.0)
    with pytest.raises(BadRequest, match="isn't flat"):
        onshape.face_normal(req(), "CYL")
    with pytest.raises(BadRequest, match="isn't on part"):
        onshape.face_normal(req(), "NOPE")


# -- printing from a loaded filament slot ------------------------------------


@pytest.fixture
def x1c_cfg(cfg: Config) -> Config:
    return with_models(
        cfg,
        X1C=ModelDefaults(
            profiles=Profiles(
                "Bambu Lab X1 Carbon 0.4 nozzle",
                "0.20mm Standard @BBL X1C",
                "Bambu PLA Basic @BBL X1C",
            )
        ),
        H2D=ModelDefaults(
            profiles=Profiles(
                "Bambu Lab H2D 0.4 nozzle", "0.20mm Standard @BBL H2D", "Bambu PLA Basic @BBL H2D"
            )
        ),
    )


def slot_plan(cfg, onshape, fake, printer, slot):
    return printing.plan_print(
        req(),
        cfg,
        onshape,
        mods(cfg, fake),
        printer,
        Orientation.parse("as-modeled"),
        PrintSettings(),
        slot,
    )


def test_ams_slot_sets_preset_colour_and_mapping(x1c_cfg: Config, onshape: OnshapeClient) -> None:
    fake = FakeBambuddy(job_states=["completed"])
    p = slot_plan(x1c_cfg, onshape, fake, "X1C_01", "0")
    assert p.profiles.filament == "Bambu ASA @BBL X1C"
    assert "AMS 1 · slot 1: ASA · yellow" in "\n".join(p.summary_lines())
    printing.execute_print(p, x1c_cfg, onshape, mods(x1c_cfg, fake), queue=True)
    assert fake.slice_bodies[0]["filament_preset"] == {
        "source": "standard",
        "id": "Bambu ASA @BBL X1C",
    }
    assert fake.slice_bodies[0]["filament_colours"] == ["#FFF144"]
    assert fake.queued[0] | {} == {
        "library_file_id": 31, "printer_id": 2, "manual_start": True,
        "ams_mapping": [0], "use_ams": True,
    }  # fmt: skip


def test_external_spool_bypasses_the_ams(cfg: Config, onshape: OnshapeClient) -> None:
    fake = FakeBambuddy(job_states=["completed"])
    p = slot_plan(cfg, onshape, fake, "A1 Mini", "254")
    assert p.profiles.filament == "Generic PETG @BBL A1M"
    printing.execute_print(p, cfg, onshape, mods(cfg, fake), queue=True)
    assert fake.queued[0]["use_ams"] is False and "ams_mapping" not in fake.queued[0]


def test_slot_refusals(x1c_cfg: Config, onshape: OnshapeClient) -> None:
    fake = FakeBambuddy()
    with pytest.raises(BadRequest, match="Nothing is loaded"):
        slot_plan(x1c_cfg, onshape, fake, "X1C_01", "3")  # the empty slot
    with pytest.raises(BadRequest, match="No slicer preset for PEEK"):
        slot_plan(x1c_cfg, onshape, fake, "X1C_01", "2")
    assert fake.queued == [] and fake.uploads == []


def test_h2d_slot_with_unknown_nozzle_is_refused(
    x1c_cfg: Config, onshape: OnshapeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests import fakes

    monkeypatch.setitem(fakes.STATUS_FILAMENT[3], "ams_extruder_map", {"0": 1})  # AMS 1 unmapped
    with pytest.raises(BadRequest, match="Can't tell which nozzle"):
        slot_plan(x1c_cfg, onshape, FakeBambuddy(), "H2D_01", "4")


def test_h2d_right_nozzle_slot_slices_with_colour(x1c_cfg: Config, onshape: OnshapeClient) -> None:
    from tests.fakes import dual_nozzle_3mf

    # group 0 on extruder_id 2 → index 1 → physical_extruder_map[1] = 0 → right nozzle
    fake = FakeBambuddy(job_states=["completed"], sliced_3mf=dual_nozzle_3mf(extruder_id=2))
    p = slot_plan(x1c_cfg, onshape, fake, "H2D_01", "4")  # AMS 2 → right
    assert p.material is not None and p.material.extruder == 0
    printing.execute_print(p, x1c_cfg, onshape, mods(x1c_cfg, fake), queue=True)
    ms = uploaded_zip(fake).read("Metadata/model_settings.config").decode()
    assert 'filament_maps" value="2"' in ms  # pinned to the right nozzle
    assert fake.slice_bodies[0]["filament_colours"] == ["#FF0000"]
    assert fake.queued[0]["ams_mapping"] == [4]


def test_h2d_left_nozzle_slot_is_checked_after_slicing(
    x1c_cfg: Config, onshape: OnshapeClient
) -> None:
    from tests.fakes import dual_nozzle_3mf

    fake = FakeBambuddy(job_states=["completed"], sliced_3mf=dual_nozzle_3mf())
    p = slot_plan(x1c_cfg, onshape, fake, "H2D_01", "0")  # AMS 1 → left nozzle
    assert p.material is not None and p.material.extruder == 1
    assert "(left nozzle)" in p.material.label
    printing.execute_print(p, x1c_cfg, onshape, mods(x1c_cfg, fake), queue=True)
    ms = uploaded_zip(fake).read("Metadata/model_settings.config").decode()
    assert 'filament_maps" value="1"' in ms and "Manual" in ms  # pinned to the left nozzle
    assert fake.queued[0]["ams_mapping"] == [0]


def test_h2d_slice_on_the_wrong_nozzle_is_not_queued(
    x1c_cfg: Config, onshape: OnshapeClient
) -> None:
    from os2slice.bambuddy import BambuddyError
    from tests.fakes import dual_nozzle_3mf

    fake = FakeBambuddy(job_states=["completed"], sliced_3mf=dual_nozzle_3mf(extruder_id=2))
    p = slot_plan(x1c_cfg, onshape, fake, "H2D_01", "254")  # Ext-L → left
    with pytest.raises(BambuddyError, match="prints on the right nozzle"):
        printing.execute_print(p, x1c_cfg, onshape, mods(x1c_cfg, fake), queue=True)
    assert fake.queued == []


def test_no_slot_keeps_the_configured_filament(cfg: Config, onshape: OnshapeClient) -> None:
    fake = FakeBambuddy(job_states=["completed"])
    p = slot_plan(cfg, onshape, fake, "A1 Mini", None)
    printing.execute_print(p, cfg, onshape, mods(cfg, fake), queue=True)
    assert p.profiles.filament == "Bambu PLA Basic @BBL A1M"
    assert "filament_colours" not in fake.slice_bodies[0]
    assert "ams_mapping" not in fake.queued[0] and "use_ams" not in fake.queued[0]


def test_plate_choice_and_config_default(cfg: Config, onshape: OnshapeClient) -> None:
    a1 = cfg.targets["bambuddy"].models["A1 Mini"]
    cfg2 = with_models(cfg, **{"A1 Mini": dataclasses.replace(a1, bed_type="Textured PEI Plate")})
    fake = FakeBambuddy(job_states=["completed"])
    p = printing.plan_print(
        req(), cfg2, onshape, mods(cfg2, fake), "A1 Mini", Orientation.parse(""), PrintSettings()
    )
    assert p.bed_type == "Textured PEI Plate"  # from config
    printing.execute_print(p, cfg2, onshape, mods(cfg2, fake), queue=False)
    assert fake.slice_bodies[-1]["bed_type"] == "Textured PEI Plate"
    p = printing.plan_print(
        req(), cfg2, onshape, mods(cfg2, fake), "A1 Mini", Orientation.parse(""), PrintSettings(),
        None, "Engineering Plate",
    )  # fmt: skip
    assert p.bed_type == "Engineering Plate"  # the user's choice wins
    with pytest.raises(BadRequest, match="Unknown build plate"):
        printing.plan_print(
            req(), cfg2, onshape, mods(cfg2, fake), "A1 Mini", Orientation.parse(""),
            PrintSettings(), None, "Glass",
        )  # fmt: skip


def test_no_plate_sends_no_bed_type(cfg: Config, onshape: OnshapeClient) -> None:
    fake = FakeBambuddy(job_states=["completed"])
    p = plan(cfg, onshape, fake)
    printing.execute_print(p, cfg, onshape, mods(cfg, fake), queue=False)
    assert "bed_type" not in fake.slice_bodies[-1]


def test_default_plate_from_print_defaults(cfg: Config, onshape: OnshapeClient) -> None:
    cfg2 = dataclasses.replace(cfg, default_bed_type="Cool Plate")
    fake = FakeBambuddy(job_states=["completed"])
    p = printing.plan_print(
        req(), cfg2, onshape, mods(cfg2, fake), "A1 Mini", Orientation.parse(""), PrintSettings()
    )
    assert p.bed_type == "Cool Plate"


# -- multi-material ----------------------------------------------------------------


def multi_plan(cfg, onshape, fake, printer, slot, extra):
    return printing.plan_print(
        req(), cfg, onshape, mods(cfg, fake), printer, Orientation.parse(""), PrintSettings(),
        slot, None, extra,
    )  # fmt: skip


def test_multi_material_on_a_single_nozzle_printer(x1c_cfg: Config, onshape: OnshapeClient) -> None:
    fake = FakeBambuddy(job_states=["completed"])
    p = multi_plan(
        x1c_cfg, onshape, fake, "X1C_01", "1", [("JKD", "0")]
    )  # PLA white base, ASA text
    assert p.multi and [c.part_id for c in p.parts] == ["JHD", "JKD"]
    assert "2 parts, one object" in "\n".join(p.summary_lines())
    printing.execute_print(p, x1c_cfg, onshape, mods(x1c_cfg, fake), queue=True)
    z = uploaded_zip(fake)
    ms = z.read("Metadata/model_settings.config").decode()
    assert ms.count("<part ") == 2 and 'key="extruder" value="2"' in ms
    assert "filament_maps" not in ms  # no nozzle map on a single-nozzle printer
    body = fake.slice_bodies[0]
    assert [f["id"] for f in body["filament_presets"]] == [
        "Bambu PLA Basic @BBL X1C",
        "Bambu ASA @BBL X1C",
    ]
    assert body["filament_colours"] == ["#FFFFFF", "#FFF144"]
    assert fake.queued[0]["ams_mapping"] == [1, 0] and fake.queued[0]["use_ams"] is True


def test_multi_material_split_across_h2d_nozzles(x1c_cfg: Config, onshape: OnshapeClient) -> None:
    from tests.fakes import dual_nozzle_3mf

    fake = FakeBambuddy(job_states=["completed"], sliced_3mf=b"")
    # base from AMS 1 (left), text from AMS 2 (right)
    p = multi_plan(x1c_cfg, onshape, fake, "H2D_01", "0", [("JKD", "4")])
    two = dual_nozzle_3mf()  # filament 1 on the left...
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(two)) as src, zipfile.ZipFile(buf, "w") as dst:
        dst.writestr(
            "Metadata/project_settings.config", src.read("Metadata/project_settings.config")
        )
        dst.writestr(
            "Metadata/slice_info.config",
            (
                '<config><plate><filament id="1" group_id="0"/><filament id="2" group_id="1"/>'
                '<nozzle id="0" extruder_id="1"/><nozzle id="1" extruder_id="2"/></plate></config>'
            ),
        )
    fake.sliced_3mf = buf.getvalue()  # ...and filament 2 on the right
    printing.execute_print(p, x1c_cfg, onshape, mods(x1c_cfg, fake), queue=True)
    ms = uploaded_zip(fake).read("Metadata/model_settings.config").decode()
    assert 'filament_maps" value="1 2"' in ms
    body = fake.slice_bodies[0]
    assert body["auto_arrange"] is False  # os2slice centred the part itself
    x = float(body["process_overrides"]["wipe_tower_x"])
    assert x >= 25 and x + bambu_project.TOWER_W <= 325  # both H2D nozzles reach it
    assert x > 175  # beside the (tiny, centred) part, on the right
    assert fake.queued[0]["ams_mapping"] == [0, 4]


def test_multi_material_refusals(x1c_cfg: Config, onshape: OnshapeClient) -> None:
    fake = FakeBambuddy()
    with pytest.raises(BadRequest, match="every part"):
        multi_plan(x1c_cfg, onshape, fake, "X1C_01", None, [("JKD", "0")])
    with pytest.raises(BadRequest, match="isn't in this Part Studio"):
        multi_plan(x1c_cfg, onshape, fake, "X1C_01", "1", [("NOPE", "0")])
    with pytest.raises(BadRequest, match="listed twice"):
        multi_plan(x1c_cfg, onshape, fake, "X1C_01", "1", [("JHD", "0")])
    assert fake.uploads == []


def test_tower_spots_avoid_the_part_and_stay_reachable() -> None:
    # The Carriage plate laid flat: 101.6 x 266.7 mm centred on the H2D bed.
    fp = (175 - 50.8, 160 - 133.35, 175 + 50.8, 160 + 133.35)
    spots = bambu_project.tower_spots("H2D", fp)
    assert spots
    for s in spots:
        x, y = float(s["wipe_tower_x"]), float(s["wipe_tower_y"])
        assert x >= 25 and x + bambu_project.TOWER_W <= 325
        overlaps_x = x < fp[2] and x + bambu_project.TOWER_W > fp[0]
        overlaps_y = y < fp[3] and y + bambu_project.TOWER_D > fp[1]
        assert not (overlaps_x and overlaps_y)
    # A part too wide for the shared area leaves no spot beside it.
    assert bambu_project.tower_spots("H2D", (20, 20, 330, 300)) == []


def test_tower_retry_on_conflict(x1c_cfg: Config, onshape: OnshapeClient) -> None:
    fake = FakeBambuddy(job_states=["completed"])
    fails = iter(["G-code conflicts detected after slicing. Please ... wipe tower ..."])

    real = fake.__call__

    def handler(request):  # first slice job fails with a tower conflict, the next succeeds
        if request.url.path == "/api/v1/slice-jobs/7":
            msg = next(fails, None)
            if msg:
                return httpx.Response(
                    200, json={"job_id": 7, "status": "failed", "error_detail": msg}
                )
        return real(request)

    from tests.fakes import dual_nozzle_multi_3mf

    fake.sliced_3mf = dual_nozzle_multi_3mf([1, 1])  # both filaments on the left nozzle
    p = multi_plan(
        x1c_cfg, onshape, fake, "H2D_01", "0", [("JKD", "254")]
    )  # both on the left nozzle
    printing.execute_print(p, x1c_cfg, onshape, mods(x1c_cfg, handler), queue=False)
    assert len(fake.slice_bodies) == 2
    first, second = (b["process_overrides"] for b in fake.slice_bodies)
    assert (
        first["wipe_tower_x"] != second["wipe_tower_x"]
        or first["wipe_tower_y"] != second["wipe_tower_y"]
    )


def test_copy_offsets_make_a_centred_grid_that_fits() -> None:
    assert bambu_project.copy_offsets((30, 20), 1, (180, 180)) == [(0.0, 0.0)]
    offs = bambu_project.copy_offsets((30, 20), 4, (180, 180), gap=6)
    assert offs == [(-18.0, -13.0), (18.0, -13.0), (-18.0, 13.0), (18.0, 13.0)]  # 2 x 2
    # A long part goes in one column of rows, not a row that would run off the bed.
    offs = bambu_project.copy_offsets((150, 20), 3, (180, 180), gap=6)
    assert {x for x, _ in offs} == {0.0} and len({y for _, y in offs}) == 3
    with pytest.raises(BadRequest, match="don't fit"):
        bambu_project.copy_offsets((100, 100), 2, (180, 180))


def test_copies_upload_one_object_placed_several_times(cfg: Config, onshape: OnshapeClient) -> None:
    fake = FakeBambuddy(job_states=["completed"])
    p = dataclasses.replace(plan(cfg, onshape, fake), settings=PrintSettings(copies=3, brim=True))
    assert "3 copies" in "\n".join(p.summary_lines())
    printing.execute_print(p, cfg, onshape, mods(cfg, fake), queue=True)
    model = uploaded_zip(fake).read("3D/3dmodel.model").decode()
    assert model.count("<item ") == 3
    body = fake.slice_bodies[0]
    assert body["auto_arrange"] is False  # os2slice laid the copies out
    assert body["process_overrides"]["brim_type"] == "outer_only"
    assert len(fake.queued) == 1  # one plate, one print


def test_too_many_copies_is_refused_before_upload(
    cfg: Config, onshape: OnshapeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(bambu_project.BED_MM, "A1 Mini", (12, 12))  # 2 mm of room: no gap fits
    fake = FakeBambuddy(job_states=["completed"])
    p = dataclasses.replace(plan(cfg, onshape, fake), settings=PrintSettings(copies=2))
    with pytest.raises(BadRequest, match="don't fit"):
        printing.execute_print(p, cfg, onshape, mods(cfg, fake), queue=True)
    assert not fake.uploads and not fake.queued


# -- the seam: another slicer feeding the BamBuddy target ----------------------------


class StandInSlicer:
    """A slicer module that isn't BamBuddy: builds the Bambu project and calls it sliced."""

    spec = ModuleSpec(
        kind="stand-in",
        label="Stand-in",
        role="slicer",
        technology="fdm",
        fields=(Field("url", "URL", "url", required=True),),
        makes=("gcode", "gcode.3mf"),
    )
    jobs: ClassVar[list[Any]] = []

    def __init__(self, values, *, key, transport=None):  # type: ignore[no-untyped-def]
        self.url = values["url"]

    def check(self):  # type: ignore[no-untyped-def]
        return Health(True, "ok")

    def profiles(self, printer_model=""):  # type: ignore[no-untyped-def]
        return ProfileCatalog()

    def slice(self, job, progress):  # type: ignore[no-untyped-def]
        self.jobs.append(job)
        progress("Slicing")
        data = bambu_project.build_project(job)
        return SliceOutput(data, f"{job.job_name}.gcode.3mf", job.media, 60, 1.5)


def test_print_through_another_slicer_and_the_bambuddy_target(
    cfg: Config, onshape: OnshapeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from os2slice.config import ModuleConfig
    from os2slice.modules import registry

    monkeypatch.setitem(registry.SLICERS, "stand-in", StandInSlicer)
    StandInSlicer.jobs = []
    a1 = cfg.targets["bambuddy"].models["A1 Mini"]
    cfg2 = dataclasses.replace(
        with_models(cfg, **{"A1 Mini": dataclasses.replace(a1, slicer="studio")}),
        slicer_modules={"studio": ModuleConfig("studio", "stand-in", {"url": "http://studio"})},
    )
    fake = FakeBambuddy(job_states=["completed"])
    m = mods(cfg2, fake)
    p = printing.plan_print(
        req(), cfg2, onshape, m, None, Orientation.parse("z+"), PrintSettings(), "254"
    )
    assert p.printer.slicer == "studio" and p.profiles.filament == "Generic PETG @BBL A1M"
    steps: list[str] = []
    out = printing.execute_print(p, cfg2, onshape, m, queue=True, progress=steps.append)
    (job,) = StandInSlicer.jobs
    assert job.media == "gcode.3mf"  # the first medium BamBuddy takes that the slicer makes
    assert job.parts[0].material is not None and job.parts[0].material.id == "254"
    assert job.printer.key == "bambuddy/1" and job.job_name.startswith("Part 1_")
    assert out.slice.print_time_s == 60 and out.submission is not None
    assert fake.slice_bodies == []  # BamBuddy didn't slice...
    assert len(fake.uploads) == 1  # ...it took the stand-in's file into its library
    assert fake.queued == [
        {"library_file_id": 30, "printer_id": 1, "manual_start": True, "use_ams": False}
    ]
    assert steps[-2:] == ["Queueing on A1 Mini", "Uploading to BamBuddy"]
