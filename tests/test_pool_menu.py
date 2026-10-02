"""The filament menu of a BamBuddy printer pool ("Any <model>"): the filaments loaded
across the model's printers first, then every filament preset made for the model."""

from __future__ import annotations

import html
import json
import re
from typing import Any

import httpx
import pytest

from os2slice import config, filaments, printing, server
from os2slice.errors import BadRequest
from os2slice.modules.bambuddy import BambuddyModule
from os2slice.modules.base import Material, PrinterInfo
from os2slice.onshape import OnshapeClient
from os2slice.orientation import Orientation
from os2slice.settings import PrintSettings
from tests.fakes import FakeBambuddy
from tests.test_modules import module
from tests.test_pools import pool, sliced
from tests.test_printing import mods, onshape, req  # noqa: F401  (onshape is a fixture)
from tests.test_server import Running, choices, panel_form, post_panel, wait_job

PRESETS = [
    "Generic PETG @BBL A1M",
    "Bambu PLA Basic @BBL A1M",
    "Generic ASA @BBL A1M",
    "My PETG @Bambu Lab A1 mini 0.4 nozzle",  # a preset saved in Bambu Studio
    "Bambu PLA Basic @BBL X1C",
    "Generic PLA @BBL H2D",
    "Generic PETG @BBL A1M",  # listed by two tiers
]


def with_presets(fake: FakeBambuddy, names: list[str]) -> httpx.MockTransport:
    """The fake, with BamBuddy's preset list replaced by `names`."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/slicer/presets":
            fake.requests.append(request)
            items = [{"id": n, "name": n} for n in names]
            empty: dict[str, list[Any]] = {"printer": [], "process": [], "filament": []}
            return httpx.Response(200, json={"standard": {**empty, "filament": items}})
        return fake(request)

    return httpx.MockTransport(handler)


def with_status(fake: FakeBambuddy, pid: int, **changes: Any) -> httpx.MockTransport:
    """The fake, with printer `pid`'s status body changed."""

    def handler(request: httpx.Request) -> httpx.Response:
        r = fake(request)
        if request.url.path == f"/api/v1/printers/{pid}/status":
            body = r.json()
            for k, v in changes.items():
                body[k] = v(body) if callable(v) else v
            return httpx.Response(200, json=body)
        return r

    return httpx.MockTransport(handler)


def view(m: BambuddyModule, printer: PrinterInfo) -> server.PrinterView:
    presets = m.filament_presets(printer)
    materials = printing.materials_of(printer, m.status(printer), None, presets)
    return server.PrinterView(printer, "idle", True, materials)


def test_dual_nozzle_pool_menu_keeps_its_loaded_filaments_usable() -> None:
    # Regression: an H2D pool's loaded filaments have no nozzle (BamBuddy picks the
    # printer, and its trays, at dispatch), and the menu disabled every one of them as
    # "(nozzle unknown)", leaving only the preset filament to pick.
    m = module(FakeBambuddy())
    v = view(m, pool(m, "H2D"))
    loaded = [c for c in server._filament_choices(v) if "." in c["value"]]
    assert [c["value"] for c in loaded] == [
        "PLA.FFFFFF", "PLA.FF0000", "PLA.000000", "PLA.0000FF",
    ]  # fmt: skip
    assert not any(c["disabled"] for c in loaded)
    assert not any("nozzle unknown" in c["label"] for c in loaded)
    assert loaded[0]["default"]  # a loaded PLA: the configured material
    page = server._printer_select([v], "Any H2D")
    assert "nozzle unknown" not in page and " disabled" not in page
    # A real dual-nozzle printer still refuses a slot whose nozzle it can't tell.
    h2d = m.printers(())[3]
    slot = Material("4", "AMS 2 · slot 1", "PLA", "#FF0000", profile="Generic PLA @BBL H2D")
    assert server._material_label(slot, h2d) == ("AMS 2 · slot 1 (nozzle unknown)", True)


def test_pool_menu_loaded_first_then_every_preset_for_the_model() -> None:
    fake = FakeBambuddy()
    m = module(fake)
    m._transport = with_presets(fake, PRESETS)
    menu = server._filament_choices(view(m, pool(m, "A1 Mini")))
    assert [(c["value"], c["label"]) for c in menu] == [
        ("PETG.000000", "PETG · black (loaded)"),
        ("", "Preset filament (Bambu PLA Basic)"),  # the configured one, in its place
        (printing.preset_material("Generic ASA @BBL A1M").id, "Generic ASA"),
        (printing.preset_material("Generic PETG @BBL A1M").id, "Generic PETG"),
        (printing.preset_material("My PETG @Bambu Lab A1 mini 0.4 nozzle").id, "My PETG"),
    ]
    assert [c["value"] for c in menu if c["default"]] == ["PETG.000000"]
    assert menu[0]["color"] == "#000000" and menu[0]["profile"] == "Generic PETG @BBL A1M"
    assert menu[2]["profile"] == "Generic ASA @BBL A1M" and not menu[2]["disabled"]
    assert not menu[2]["color"]
    assert all(re.fullmatch(server.MATERIAL_ID, c["value"]) for c in menu if c["value"])
    # Real printers keep their menu: the preset first, then what is loaded.
    real = m.printers(())[0]
    assert [c["value"] for c in server._filament_choices(view(m, real))] == ["", "254"]
    # Both menus came from BamBuddy's cached preset list: one fetch.
    assert sum(r.url.path == "/api/v1/slicer/presets" for r in fake.requests) == 1


def test_pool_menu_defaults_to_the_preset_when_nothing_is_loaded() -> None:
    fake = FakeBambuddy()
    m = module(fake)
    m._transport = with_status(fake, 1, vt_tray=[])
    menu = server._filament_choices(view(m, pool(m, "A1 Mini")))
    assert menu[0] == {
        "value": "", "label": "Preset filament (Bambu PLA Basic)", "default": True,
        "tool": False, "profile": "Bambu PLA Basic @BBL A1M",
    }  # fmt: skip
    assert [c["label"] for c in menu[1:]] == ["Generic PETG"]
    assert not any(c["default"] for c in menu[1:])


def test_pool_tray_without_a_colour_matches_by_type_only() -> None:
    fake = FakeBambuddy()
    m = module(fake)
    m._transport = with_status(
        fake, 1, vt_tray=lambda body: [{**body["vt_tray"][0], "tray_color": ""}]
    )
    a1 = pool(m, "A1 Mini")
    (petg,) = m.status(a1).materials
    assert (petg.id, petg.colour, petg.label) == ("PETG.ANY", None, "PETG · any colour (loaded)")
    assert petg.profile == "Generic PETG @BBL A1M"
    assert re.fullmatch(server.MATERIAL_ID, petg.id)
    m.submit(a1, sliced(m), start=True, materials=(petg,))
    assert "filament_overrides" not in fake.queued[0]  # not forced onto a made-up grey


def test_pool_presets_fail_soft(cfg: config.Config) -> None:
    fake = FakeBambuddy()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/slicer/presets":
            return httpx.Response(500, json={"detail": "boom"})
        return fake(request)

    m = mods(cfg, handler)
    assert m.pool_presets(m.find("Any A1 Mini")) == ()
    m = mods(cfg, fake)
    assert m.pool_presets(m.find("Any A1 Mini")) == (
        "Bambu PLA Basic @BBL A1M",
        "Generic PETG @BBL A1M",
    )
    assert m.pool_presets(m.find("A1 Mini")) == ()  # only pools offer presets


def test_compatible_presets_by_name() -> None:
    names = [
        "b @BBL A1M", "A @BBL A1M", "c @BBL A1M2", "d @Bambu Lab A1 mini 0.4 nozzle",
        "A @BBL A1M", "no suffix",
    ]  # fmt: skip
    assert filaments.compatible_presets(names, "@BBL A1M", "Bambu Lab A1 mini 0.4 nozzle") == [
        "A @BBL A1M", "b @BBL A1M", "d @Bambu Lab A1 mini 0.4 nozzle",
    ]  # fmt: skip
    assert filaments.compatible_presets(names, "@BBL A1M") == ["A @BBL A1M", "b @BBL A1M"]
    assert filaments.compatible_presets(names, "", "") == []
    assert filaments.preset_label("Generic PETG @BBL A1M") == "Generic PETG"
    assert filaments.preset_label("Plain") == "Plain"


def test_preset_choice_on_a_pool_slices_with_it_and_forces_nothing(
    cfg: config.Config,
    onshape: OnshapeClient,  # noqa: F811
) -> None:
    fake = FakeBambuddy(job_states=["completed"])
    m = mods(cfg, fake)
    choice = printing.preset_material("Generic PETG @BBL A1M").id
    p = printing.plan_print(req(), cfg, onshape, m, "Any A1 Mini", Orientation.parse("z+"),
                            PrintSettings(), choice)  # fmt: skip
    assert p.profiles.filament == "Generic PETG @BBL A1M"
    assert p.material is not None and p.material.label == "Generic PETG"
    printing.execute_print(p, cfg, onshape, m, queue=True)
    assert fake.slice_bodies[0]["filament_preset"] == {
        "source": "standard",
        "id": "Generic PETG @BBL A1M",
    }
    assert not fake.slice_bodies[0].get("filament_colours")
    assert fake.queued == [
        {"library_file_id": 31, "target_model": "A1 Mini", "manual_start": False}
    ]


def test_preset_choice_is_only_offered_on_pools(
    cfg: config.Config,
    onshape: OnshapeClient,  # noqa: F811
) -> None:
    m = mods(cfg, FakeBambuddy())
    choice = printing.preset_material("Generic PETG @BBL A1M").id
    with pytest.raises(BadRequest, match="Nothing is loaded"):
        printing.plan_print(req(), cfg, onshape, m, "A1 Mini", Orientation.parse("z+"),
                            PrintSettings(), choice)  # fmt: skip


def test_panel_offers_and_posts_a_pool_preset(cfg: config.Config) -> None:
    run = Running(cfg, FakeBambuddy(job_states=["completed"]))
    try:
        r, form = panel_form(run)
        menu = re.search(r'<select name="filament" data-choices="([^"]*)"', r.text)
        assert menu, r.text
        pool_menu = json.loads(html.unescape(menu.group(1)))["bambuddy/any:A1 Mini"]
        assert [c["label"] for c in pool_menu] == [
            "PETG · black (loaded)", "Preset filament (Bambu PLA Basic)", "Generic PETG",
        ]  # fmt: skip
        chosen = choices(printer="bambuddy/any:A1 Mini", filament=pool_menu[2]["value"])
        post = {**form, "p": "JHD", **chosen}
        r = post_panel(run, post)
        assert r.status_code == 303, r.text
        wait_job(run, r.headers["Location"])
        (item,) = run.fake.queued
        assert item["target_model"] == "A1 Mini" and "filament_overrides" not in item
        assert run.fake.slice_bodies[0]["filament_preset"]["id"] == "Generic PETG @BBL A1M"
    finally:
        run.httpd.shutdown()
