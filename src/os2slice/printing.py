"""The BamBuddy print path: plan (read-only) → confirm → export, orient, upload, slice, queue.

`plan_print` only reads, so it's safe to run for a confirmation page or a
CLI prompt. `execute_print` does the work and is only called after an
explicit human confirmation (D-13).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from os2slice import files
from os2slice.bambuddy import BambuddyClient, BambuddyError, PresetChoice, Printer, SliceResult
from os2slice.config import Config
from os2slice.errors import BadRequest, ConfigError
from os2slice.filaments import (
    NOZZLE_NAMES,
    Slot,
    match_preset,
    preset_suffix,
    sliced_nozzle,
    slots_from_status,
)
from os2slice.onshape import OnshapeClient
from os2slice.orientation import (
    AXES,
    IDENTITY,
    Matrix,
    Orientation,
    bounding_box,
    face_down,
    orient_parts,
    orient_stl,
    translate_xy,
)
from os2slice.request import ExportRequest
from os2slice.settings import PrintSettings, check_bed_type
from os2slice.threemf import Part, build_3mf, project_settings_of, with_project_settings

log = logging.getLogger(__name__)

Progress = Callable[[str], None]

# Multi-filament prints are placed at the bed centre by os2slice (auto-arrange off), so the
# prime tower can go beside the part. On dual-nozzle printers it must also be where both
# nozzles reach (H2D: left x 0-325, right x 25-350; docs/BAMBUDDY_API.md).
BED_MM = {"H2D": (350, 320), "H2D Pro": (350, 320), "H2S": (340, 320), "H2C": (350, 320),
          "A1 Mini": (180, 180)}  # fmt: skip
SHARED_X = {"H2D": (25, 325), "H2D Pro": (25, 325), "H2C": (25, 325)}
TOWER_W, TOWER_D, GAP = 40.0, 60.0, 12.0  # generous tower footprint and clearance, mm
TOWER_ERRORS = ("conflict", "unprintable area", "wipe tower", "prime tower")
# Copies: gap between neighbours (more with a brim, Bambu's default brim is 5 mm wide)
# and a margin kept clear around the bed's edge, mm.
COPY_GAP, BRIM_GAP, BED_MARGIN = 6.0, 10.0, 5.0


def copy_offsets(
    size: tuple[float, float], copies: int, bed: tuple[float, float], gap: float = COPY_GAP
) -> list[tuple[float, float]]:
    """X/Y offsets that lay `copies` of a `size` (w, d) footprint out in a grid centred
    on its current position, row by row from the front left; the squarest grid that fits.
    """
    w, d = size
    room_w, room_d = bed[0] - 2 * BED_MARGIN, bed[1] - 2 * BED_MARGIN
    best: tuple[float, int, int] | None = None
    for cols in range(1, copies + 1):
        rows = -(-copies // cols)
        grid_w, grid_d = cols * w + (cols - 1) * gap, rows * d + (rows - 1) * gap
        if grid_w <= room_w and grid_d <= room_d:
            score = max(grid_w / room_w, grid_d / room_d)
            if best is None or score < best[0]:
                best = (score, cols, rows)
    if best is None:
        raise BadRequest(
            f"{copies} copies don't fit on the plate",
            f"Each copy is {w:.0f} x {d:.0f} mm; print fewer copies",
        )
    _, cols, rows = best
    pitch_x, pitch_y = w + gap, d + gap
    x0, y0 = -(cols - 1) * pitch_x / 2, -(rows - 1) * pitch_y / 2
    return [(x0 + (n % cols) * pitch_x, y0 + (n // cols) * pitch_y) for n in range(copies)]


def tower_spots(model: str, footprint: tuple[float, float, float, float]) -> list[dict[str, str]]:
    """Prime tower positions beside a part footprint (x0, y0, x1, y1), best first."""
    w, d = BED_MM.get(model, (256, 256))
    lo_x, hi_x = SHARED_X.get(model, (0, w))
    x0, y0, x1, y1 = footprint
    cy = min(max((y0 + y1) / 2 - TOWER_D / 2, 5), d - TOWER_D - 5)
    cx = (x0 + x1) / 2 - TOWER_W / 2
    spots = [
        (x1 + GAP, max(y1 - TOWER_D, 5)),  # right, towards the back (like Bambu Studio)
        (x0 - GAP - TOWER_W, max(y1 - TOWER_D, 5)),  # left
        (x1 + GAP, cy),  # right, middle
        (cx, y1 + GAP),  # behind
        (cx, y0 - GAP - TOWER_D),  # in front
    ]
    ok = [
        (x, y) for x, y in spots
        if lo_x <= x and x + TOWER_W <= hi_x and y >= 0 and y + TOWER_D <= d
    ]  # fmt: skip
    return [{"wipe_tower_x": f"{x:.1f}", "wipe_tower_y": f"{y:.1f}"} for x, y in ok]


@dataclass(frozen=True)
class PartChoice:
    """One part of the print and the filament it prints with."""

    part_id: str
    name: str
    slot: Slot | None
    filament_preset: str

    def describe(self) -> str:
        return f"{self.name}: {self.slot.describe() if self.slot else self.filament_preset}"


@dataclass(frozen=True)
class PrintPlan:
    req: ExportRequest
    document_name: str
    part_name: str
    printer: Printer
    printer_state: str
    presets: PresetChoice
    orientation: Orientation
    settings: PrintSettings
    manual_start: bool
    slot: Slot | None = None  # loaded filament to print from; None = preset filament
    bed_type: str | None = None  # build plate; None = the process preset's default
    extra: tuple[PartChoice, ...] = ()  # further parts of a multi-material print

    @property
    def parts(self) -> tuple[PartChoice, ...]:
        first = PartChoice(self.req.part_id or "", self.part_name, self.slot, self.presets.filament)
        return (first, *self.extra)

    @property
    def multi(self) -> bool:
        return bool(self.extra)

    def summary_lines(self) -> list[str]:
        if self.multi:
            what = [f"Parts:       {len(self.parts)} parts, one object  ({self.document_name})"]
            what += [f"  · {p.describe()}" for p in self.parts]
        else:
            what = [
                f"Part:        {self.part_name}  ({self.document_name})",
                "Filament:    "
                + (self.slot.describe() if self.slot else "as the preset (BamBuddy maps it)"),
            ]
        lines = [
            *what,
            f"Printer:     {self.printer.name} ({self.printer.model}), now {self.printer_state}",
            f"Presets:     {self.presets.process} / {self.presets.filament}",
            f"Plate:       {self.bed_type or 'process preset default'}",
            f"Orientation: {self.orientation.describe()}",
            f"Settings:    {self.settings.describe()}",
            "Start:       "
            + (
                "waits in BamBuddy's queue until you press Start"
                if self.manual_start
                else "starts as soon as the printer is free"
            ),
        ]
        if self.req.configuration:
            lines.insert(1, f"Config:      {self.req.configuration}")
        return lines


@dataclass(frozen=True)
class PrintOutcome:
    slice: SliceResult
    queue_item: dict[str, Any] | None  # None when only sliced
    threemf: bytes | None = None  # the uploaded 3MF (3MF path only)

    @property
    def queued(self) -> bool:
        return self.queue_item is not None


def rotation_for(
    orientation: Orientation,
    onshape: OnshapeClient,
    req: ExportRequest,
    part_ids: list[str] | None = None,
) -> Matrix:
    """The rotation to apply before upload. `auto` leaves the part as modeled for the slicer.

    With several parts (`part_ids`), the face may belong to any of them.
    """
    if orientation.kind == "face":
        owner = req.part_id
        if part_ids and len(part_ids) > 1:
            owner = onshape.part_of_face(req, orientation.value)
            if owner not in part_ids:
                raise BadRequest("The selected face isn't on any of the selected parts")
        return face_down(onshape.face_normal(replace(req, part_id=owner), orientation.value))
    if orientation.kind == "axis":
        return face_down(AXES[orientation.value])
    return IDENTITY


def oriented_stl(orientation: Orientation, onshape: OnshapeClient, req: ExportRequest) -> bytes:
    """Export the part and orient it; used by the preview (read-only)."""
    rotation = rotation_for(orientation, onshape, req)
    return orient_stl(onshape.export_stl(req, units="millimeter"), rotation)


def oriented_parts(
    orientation: Orientation, onshape: OnshapeClient, req: ExportRequest, part_ids: list[str]
) -> list[bytes]:
    """Several parts oriented and dropped together; used by the preview (read-only)."""
    rotation = rotation_for(orientation, onshape, req, part_ids)
    stls = [onshape.export_stl(replace(req, part_id=p), units="millimeter") for p in part_ids]
    return orient_parts(stls, rotation)


def resolve_printer(printers: list[Printer], wanted: str | None) -> Printer:
    active = [p for p in printers if p.is_active]
    listing = ", ".join(f"{p.name} (#{p.id})" for p in active) or "none"
    if not wanted:
        raise BadRequest("No printer chosen", f"Pick one of: {listing}")
    for p in active:
        if wanted == p.name or wanted == str(p.id):
            return p
    raise BadRequest(f"No active printer called {wanted!r}", f"Printers: {listing}")


def plan_print(
    req: ExportRequest,
    cfg: Config,
    onshape: OnshapeClient,
    bambuddy: BambuddyClient,
    printer: str | None,
    orientation: Orientation,
    settings: PrintSettings,
    slot: int | None = None,
    bed_type: str | None = None,
    extra_parts: list[tuple[str, int]] | None = None,
) -> PrintPlan:
    """Gather everything for the confirmation. Reads from Onshape and BamBuddy only.

    `slot` is a global tray id (docs/BAMBUDDY_API.md); the filament preset then
    follows the loaded material instead of the configured one. `extra_parts` makes
    it a multi-material print: more (part id, slot) pairs printed as one object.
    """
    if cfg.bambuddy is None:
        raise ConfigError("BamBuddy isn't configured", f"Add a [bambuddy] table to {cfg.path}")
    if req.part_id is None:
        raise BadRequest("Whole Part Studio printing isn't implemented yet", "Pick one part")
    chosen = resolve_printer(bambuddy.list_printers(), printer or cfg.bambuddy.default_printer)
    presets = cfg.bambuddy.presets.get(chosen.model)
    if presets is None:
        raise ConfigError(
            f"No slicer presets configured for printer model {chosen.model!r}",
            f'Add [bambuddy.presets."{chosen.model}"] to {cfg.path}',
        )
    status = bambuddy.printer_status(chosen.id)
    dual = chosen.nozzle_count > 1
    slots = {s.tray_id: s for s in slots_from_status(status, dual)}
    names: list[str] = []
    suffix = preset_suffix(presets.filament)

    def resolve(tray: int) -> tuple[Slot, str]:
        loaded = slots.get(tray)
        if loaded is None:
            raise BadRequest(f"Nothing is loaded in slot {tray} on {chosen.name}")
        if dual and loaded.extruder not in NOZZLE_NAMES:
            raise BadRequest(
                f"Can't tell which nozzle {loaded.label} on {chosen.name} feeds",
                "Check the AMS assignment on the printer, or use the preset filament",
            )
        if not names:
            names.extend(bambuddy.filament_preset_names())
        preset = match_preset(loaded, suffix, names)
        if preset is None:
            raise BadRequest(f"No slicer preset for {loaded.material} on {chosen.name}")
        return loaded, preset

    loaded: Slot | None = None
    if slot is not None:
        loaded, preset = resolve(slot)
        presets = replace(presets, filament=preset)
    extra: list[PartChoice] = []
    if extra_parts:
        if loaded is None:
            raise BadRequest(
                "Pick a loaded filament for every part of a multi-material print",
                "Choose a slot, not the preset filament",
            )
        part_names = {str(p.get("partId")): str(p.get("name")) for p in onshape.list_parts(req)}
        seen = {req.part_id}
        for part_id, tray in extra_parts:
            if part_id in seen:
                raise BadRequest(f"Part {part_id!r} is listed twice")
            if part_id not in part_names:
                raise BadRequest(f"Part {part_id!r} isn't in this Part Studio")
            seen.add(part_id)
            s, p = resolve(tray)
            extra.append(PartChoice(part_id, part_names[part_id], s, p))
    state = str(status.get("state") or ("offline" if not status.get("connected") else "unknown"))
    if status.get("awaiting_plate_clear"):
        state += ", waiting for the plate to be cleared"
    return PrintPlan(
        req=req,
        document_name=onshape.get_document_name(req.document_id),
        part_name=onshape.get_part_name(req),
        printer=chosen,
        printer_state=state,
        presets=presets,
        orientation=orientation,
        settings=settings,
        manual_start=cfg.bambuddy.manual_start,
        slot=loaded,
        bed_type=check_bed_type(bed_type) or presets.bed_type or cfg.default_bed_type,
        extra=tuple(extra),
    )


def execute_print(
    plan: PrintPlan,
    cfg: Config,
    onshape: OnshapeClient,
    bambuddy: BambuddyClient,
    *,
    queue: bool,
    progress: Progress = lambda s: None,
    now: datetime | None = None,
    force_3mf: bool = False,
) -> PrintOutcome:
    """Export → orient → upload → slice → (queue). Call only after explicit confirmation."""
    if cfg.bambuddy is None:
        raise ConfigError("BamBuddy isn't configured", f"Add a [bambuddy] table to {cfg.path}")
    req = plan.req

    progress("Working out the orientation")
    rotation = rotation_for(plan.orientation, onshape, req, [p.part_id for p in plan.parts])
    root = bambuddy.ensure_folder(cfg.bambuddy.folder)
    folder = bambuddy.ensure_folder(files.sanitize(plan.document_name, "document"), root)
    dual = plan.printer.nozzle_count > 1
    copies = plan.settings.copies > 1  # laid out by os2slice, in a 3MF
    as_3mf = force_3mf or plan.multi or copies or (dual and plan.slot is not None)
    threemf = None

    if not as_3mf:
        progress("Exporting the part from Onshape")
        stl = orient_stl(onshape.export_stl(req, units="millimeter"), rotation)
        progress("Uploading to BamBuddy")
        name = _file_name(cfg, plan, "stl", now)
        file_id = bambuddy.upload(folder, name, stl)
        progress("Slicing")
        job = bambuddy.start_slice(
            file_id,
            plan.presets,
            plan.settings,
            auto_orient=plan.orientation.kind == "auto",
            filament_colours=[plan.slot.color] if plan.slot else None,
            bed_type=plan.bed_type,
        )
        filament_slots = [plan.slot] if plan.slot else []
    else:
        progress(f"Exporting {len(plan.parts)} part(s) from Onshape")
        asm = assemble(plan, onshape, rotation)
        filament_slots = asm.slots
        threemf = asm.threemf
        presets_by_filament = asm.presets
        # One filament: no tower needed. Several: try spots beside the part in turn.
        towers: list[dict[str, str]] = [{}]
        if len(filament_slots) > 1:
            towers = tower_spots(plan.printer.model, asm.footprint) or [{}]
        progress("Uploading to BamBuddy")
        name = _file_name(cfg, plan, "3mf", now)
        file_id = bambuddy.upload(folder, name, asm.threemf)
        for attempt, tower in enumerate(towers, start=1):
            progress("Slicing" if attempt == 1 else f"Slicing again, prime tower moved ({attempt})")
            job = bambuddy.start_slice(
                file_id,
                plan.presets,
                plan.settings,
                auto_orient=plan.orientation.kind == "auto",
                filament_colours=[s.color for s in filament_slots],
                bed_type=plan.bed_type,
                filament_presets=presets_by_filament,
                extra_overrides=tower,
                auto_arrange=plan.orientation.kind == "auto",
            )
            try:
                sliced = bambuddy.wait_for_slice(job, on_status=lambda s: progress(f"Slicing: {s}"))
                break
            except BambuddyError as e:
                tower_problem = any(w in e.message.lower() for w in TOWER_ERRORS)
                if not tower_problem or attempt == len(towers):
                    raise
                log.info("tower at %s rejected: %s", tower, e.message)

    if not as_3mf:
        sliced = bambuddy.wait_for_slice(job, on_status=lambda s: progress(f"Slicing: {s}"))
    log.info("sliced %s → library file %s", name, sliced.library_file_id)
    if dual and filament_slots and all(s.extruder is not None for s in filament_slots):
        # Every filament must print on the nozzle its slot feeds.
        data = bambuddy.download_file(sliced.library_file_id)
        for n, s in enumerate(filament_slots, start=1):
            got = sliced_nozzle(data, n)
            if got != s.extruder:
                raise BambuddyError(
                    f"Filament {n} prints on the {NOZZLE_NAMES.get(got, 'unknown')} nozzle, but "
                    f"{s.label} feeds the {NOZZLE_NAMES[s.extruder]} one; not queued",  # type: ignore[index]
                    "BamBuddy's slicer changed how it maps nozzles; see docs/BAMBUDDY_API.md",
                )

    if not queue:
        return PrintOutcome(sliced, None, threemf)
    progress(f"Queueing on {plan.printer.name}")
    mapping, use_ams = None, None
    if len(filament_slots) == 1 and filament_slots[0].external:
        mapping, use_ams = None, False  # one external spool: bypass the AMS (to verify)
    elif filament_slots:
        mapping, use_ams = [s.tray_id for s in filament_slots], True
    item = bambuddy.queue_print(
        sliced.library_file_id, plan.printer.id, plan.manual_start, mapping, use_ams
    )
    return PrintOutcome(sliced, item, threemf)


def studio_project(
    plan: PrintPlan,
    cfg: Config,
    onshape: OnshapeClient,
    bambuddy: BambuddyClient,
    progress: Progress = lambda s: None,
) -> bytes:
    """A Bambu Studio project of the plan: its geometry plus the printer, filament and
    process settings BamBuddy sliced it with (presets, colours, plate, walls/infill/
    supports, prime tower). Slices but never queues. Falls back to geometry only (logged)
    when the slice fails, so the part still opens.
    """
    try:
        outcome = execute_print(
            plan, cfg, onshape, bambuddy, queue=False, progress=progress, force_3mf=True
        )
        if outcome.threemf is None:
            raise ValueError("no 3MF was built")
        sliced = bambuddy.download_file(outcome.slice.library_file_id)
        return with_project_settings(outcome.threemf, project_settings_of(sliced))
    except (BambuddyError, ValueError) as e:
        log.warning("opening %s without slicer settings: %s", plan.part_name, e)
        return assemble(plan, onshape).threemf


@dataclass(frozen=True)
class Assembly:
    threemf: bytes  # the parts as one object (one instance per copy), centred on the bed
    slots: list[Slot]  # filament n is slots[n-1] (empty when printing with the preset)
    presets: list[str]  # filament preset per filament
    footprint: tuple[float, float, float, float]  # x0, y0, x1, y1 of all copies on the bed, mm


def assemble(plan: PrintPlan, onshape: OnshapeClient, rotation: Matrix | None = None) -> Assembly:
    """Export the plan's parts and pack them into one Bambu-style 3MF object.

    Used both to print (uploaded to BamBuddy) and to open in Bambu Studio. Parts
    share one rotation and one drop; the assembly (or the grid of its copies) is
    centred on the printer's bed;
    each distinct slot becomes a filament, pinned to its nozzle on dual-nozzle
    printers. Without slots every part is filament 1.
    """
    req = plan.req
    if rotation is None:
        rotation = rotation_for(plan.orientation, onshape, req, [p.part_id for p in plan.parts])
    stls = [
        onshape.export_stl(replace(req, part_id=p.part_id), units="millimeter") for p in plan.parts
    ]
    placed = orient_parts(stls, rotation)
    bed_w, bed_d = BED_MM.get(plan.printer.model, (256, 256))
    boxes = [bounding_box(s) for s in placed]
    x0, y0 = min(b[0][0] for b in boxes), min(b[0][1] for b in boxes)
    x1, y1 = max(b[1][0] for b in boxes), max(b[1][1] for b in boxes)
    dx, dy = bed_w / 2 - (x0 + x1) / 2, bed_d / 2 - (y0 + y1) / 2
    placed = [translate_xy(s, dx, dy) for s in placed]
    gap = COPY_GAP + (BRIM_GAP if plan.settings.brim else 0.0)
    offsets = copy_offsets((x1 - x0, y1 - y0), plan.settings.copies, (bed_w, bed_d), gap)
    slots: list[Slot] = []
    presets: list[str] = []
    index: dict[int, int] = {}
    for p in plan.parts:
        if p.slot is not None and p.slot.tray_id not in index:
            index[p.slot.tray_id] = len(slots) + 1
            slots.append(p.slot)
            presets.append(p.filament_preset)
    if not slots:
        presets = [plan.presets.filament]
    parts = [
        Part(p.name, stl, index[p.slot.tray_id] if p.slot else 1)
        for p, stl in zip(plan.parts, placed, strict=True)
    ]
    dual = plan.printer.nozzle_count > 1
    maps = [1 if s.extruder == 1 else 2 for s in slots] if dual and slots else None
    ox0, oy0 = min(o[0] for o in offsets), min(o[1] for o in offsets)
    ox1, oy1 = max(o[0] for o in offsets), max(o[1] for o in offsets)
    return Assembly(
        build_3mf(parts, plan.part_name, maps, offsets),
        slots,
        presets,
        (x0 + dx + ox0, y0 + dy + oy0, x1 + dx + ox1, y1 + dy + oy1),
    )


def _file_name(cfg: Config, plan: PrintPlan, ext: str, now: datetime | None) -> str:
    return files.export_path(
        cfg.export_dir, plan.document_name, plan.part_name, plan.req.configuration, ext, now
    ).name
