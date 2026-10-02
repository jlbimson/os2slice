"""The print path: plan (read-only) → confirm → export, orient, slice, submit.

`plan_print` only reads, so it's safe to run for a confirmation page or a CLI
prompt. `execute_print` does the work and is only called after an explicit human
confirmation (D-13). Slicing and submitting go through the printer's slicer and
target modules (docs/MODULES.md, D-27); this file knows no service by name.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime

from os2slice import files
from os2slice.config import Config
from os2slice.errors import BadRequest, ConfigError
from os2slice.modules import bambu_project
from os2slice.modules.base import (
    MEDIA_GCODE_3MF,
    Material,
    Media,
    ModuleError,
    ModuleSpec,
    PartGeometry,
    PrinterInfo,
    PrinterStatus,
    ProfileCatalog,
    Profiles,
    SliceInput,
    SliceOutput,
    Submission,
    distinct_materials,
)
from os2slice.modules.registry import Modules
from os2slice.onshape import OnshapeClient
from os2slice.orientation import (
    AXES,
    IDENTITY,
    Matrix,
    Orientation,
    face_down,
    orient_parts,
    orient_stl,
)
from os2slice.request import ExportRequest
from os2slice.settings import PrintSettings, check_bed_type
from os2slice.threemf import orca_version_of, project_settings_of, with_project_settings

log = logging.getLogger(__name__)

Progress = Callable[[str], None]


@dataclass(frozen=True)
class PartChoice:
    """One part of the print and the material it prints with."""

    part_id: str
    name: str
    material: Material | None
    filament_profile: str

    def describe(self) -> str:
        return f"{self.name}: {self.material.label if self.material else self.filament_profile}"


@dataclass(frozen=True)
class PrintPlan:
    req: ExportRequest
    document_name: str
    part_name: str
    printer: PrinterInfo
    status: PrinterStatus
    profiles: Profiles  # resolved: the filament follows the chosen material
    orientation: Orientation
    settings: PrintSettings
    manual_start: bool  # the target leaves the print waiting for a person (D-13)
    target_label: str = ""  # "BamBuddy"
    material: Material | None = None  # loaded material to print from; None = profile filament
    bed_type: str | None = None  # build plate; None = the process profile's default
    extra: tuple[PartChoice, ...] = ()  # further parts of a multi-material print
    # A filament changer's tools, all of them in tool order, when the parts print from
    # tools: the slicer loads every one so T<n> stays the changer's tool n.
    tools: tuple[Material, ...] = ()

    @property
    def parts(self) -> tuple[PartChoice, ...]:
        first = PartChoice(
            self.req.part_id or "", self.part_name, self.material, self.profiles.filament
        )
        return (first, *self.extra)

    @property
    def multi(self) -> bool:
        return bool(self.extra)

    def to_load(self) -> list[str]:
        """'T2: load PM ASA (now CR-PETG Transparent)' for tools the user assigned."""
        return [
            f"T{m.raw['tool']}: load {m.profile} (now {m.raw['replaces']})"
            for m in self.tools
            if m.raw.get("planned")
        ]

    def summary_lines(self) -> list[str]:
        if self.multi:
            what = [f"Parts:       {len(self.parts)} parts, one object  ({self.document_name})"]
            what += [f"  · {p.describe()}" for p in self.parts]
        else:
            what = [
                f"Part:        {self.part_name}  ({self.document_name})",
                "Filament:    "
                + (
                    self.material.label
                    if self.material
                    else f"as the preset ({self.target_label} maps it)"
                ),
            ]
        lines = [
            *what,
            f"Printer:     {self.printer.name} ({self.printer.model}), "
            f"now {self.status.describe()}",
            f"Presets:     {self.profiles.printer} / {self.profiles.process} / "
            f"{self.profiles.filament}",
            *(f"Before start: {line}" for line in self.to_load()),
            f"Plate:       {self.bed_type or 'process preset default'}",
            f"Orientation: {self.orientation.describe()}",
            f"Settings:    {self.settings.describe()}",
            "Start:       "
            + (
                f"waits in {self.target_label}'s queue until you press Start"
                if self.manual_start
                else "starts as soon as the printer is free"
            ),
        ]
        if self.req.configuration:
            lines.insert(1, f"Config:      {self.req.configuration}")
        return lines


@dataclass(frozen=True)
class PrintOutcome:
    input: SliceInput  # what was sliced (the oriented parts, profiles, materials)
    slice: SliceOutput
    submission: Submission | None  # None when only sliced

    @property
    def queued(self) -> bool:
        return self.submission is not None


def rotation_for(
    orientation: Orientation,
    onshape: OnshapeClient,
    req: ExportRequest,
    part_ids: list[str] | None = None,
) -> Matrix:
    """The rotation to apply before slicing. `auto` leaves the part as modeled.

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


def has_profiles(printer: PrinterInfo) -> bool:
    """Whether a printer can be sliced for: it has a slicer and profiles to slice with."""
    p = printer.profiles
    return bool(printer.slicer and (p.printer or p.process or p.filament))


def materials_of(
    printer: PrinterInfo, status: PrinterStatus, own: ProfileCatalog | None = None
) -> tuple[Material, ...]:
    """What the printer has loaded; when the target can't tell, its configured materials,
    else the user's own filament profiles in its slicer (`own`), one choice each.

    Loaded materials without a profile get the user's best match (`match_profile`). A
    filament changer's tools (Material.raw["tool"]) come first, then the own profiles,
    for prints that don't use the changer.
    """
    loaded = status.materials or printer.materials
    if own is not None and own.filament_types:
        loaded = tuple(
            m
            if m.profile or m.raw.get("empty")
            else replace(m, profile=match_profile(m, printer, own))
            for m in loaded
        )
    if loaded and not any(is_tool(m) for m in loaded):
        return loaded
    colours = own.filament_colours if own else {}
    return loaded + tuple(
        profile_material(name, colours.get(name)) for name in (own.filament if own else ())
    )


# A filament profile assigned to a changer tool from the panel: "t<tool>.<material id>"
# (or ".preset", the printer's own filament).
SLOT_RE = re.compile(r"t(\d{1,2})\.(f-[0-9a-f]{12}|preset)")


def is_tool(material: Material) -> bool:
    return "tool" in material.raw


def match_profile(material: Material, printer: PrinterInfo, own: ProfileCatalog) -> str:
    """The user's filament profile for a loaded material: one of its type, preferring a
    name with the spool's vendor in it, then words of the filament's name, then the
    printer's own filament, then the first by name. "" when none has that type."""
    kind = material.kind.casefold()
    candidates = sorted(
        (n for n, t in own.filament_types.items() if kind and t.casefold() == kind),
        key=str.casefold,
    )
    if not candidates:
        return ""
    vendor = str(material.raw.get("vendor") or "").casefold()
    name_words = set(_words(str(material.raw.get("name") or ""))) - {kind}

    def score(name: str) -> tuple[bool, int, bool]:
        words = set(_words(name))
        return (
            bool(vendor) and vendor in name.casefold(),
            len(words & name_words),
            name == printer.profiles.filament,
        )

    return max(candidates, key=score)  # max keeps the first of equals: by name


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.casefold())


def profile_material(profile: str, colour: str | None = None) -> Material:
    """A filament profile offered as a material. Its id is stable while the name is."""
    digest = hashlib.sha256(profile.encode()).hexdigest()[:12]
    return Material(id=f"f-{digest}", label=profile, colour=colour, profile=profile)


def usable(material: Material, printer: PrinterInfo) -> bool:
    """A material can be printed from once it has a profile and, with two nozzles, a nozzle."""
    return bool(material.profile) and (printer.nozzle_count <= 1 or material.extruder is not None)


def media_for(slicer: ModuleSpec, target: ModuleSpec) -> Media:
    """The first medium the target accepts that the slicer makes."""
    for m in target.accepts:
        if m in slicer.makes:
            return m
    raise ConfigError(f"{slicer.label} can't make anything {target.label} takes")


def plan_print(
    req: ExportRequest,
    cfg: Config,
    onshape: OnshapeClient,
    modules: Modules,
    printer: str | None,
    orientation: Orientation,
    settings: PrintSettings,
    material: str | None = None,
    bed_type: str | None = None,
    extra_parts: list[tuple[str, str]] | None = None,
    process: str | None = None,
    machine: str | None = None,
) -> PrintPlan:
    """Gather everything for the confirmation. Reads from Onshape and the target only.

    `printer` is a PrinterInfo key or name (default: `default_printer`). `material`
    is a Material.id from the printer's status (a global tray id on BamBuddy); the
    filament profile then follows the loaded material instead of the configured one.
    `extra_parts` makes it a multi-material print: more (part id, material id) pairs
    printed as one object. `process` and `machine` pick one of the user's own process and
    printer profiles in the printer's slicer instead of the configured ones.
    """
    if not modules.targets:
        raise ConfigError(
            "No printers are configured", f"Add a [targets.<name>] table to {cfg.path}"
        )
    if req.part_id is None:
        raise BadRequest("Whole Part Studio printing isn't implemented yet", "Pick one part")
    chosen = modules.find(printer)
    if not has_profiles(chosen):
        raise ConfigError(
            f"No slicer presets configured for printer model {chosen.model!r}",
            f'Add [targets.{chosen.target}.models."{chosen.model}"] to {cfg.path}',
        )
    target = modules.target_for(chosen)
    status = target.status(chosen)
    own = modules.own_profiles(chosen)
    loaded = {m.id: m for m in materials_of(chosen, status, own)}
    dual = chosen.nozzle_count > 1

    def resolve(mid: str) -> Material:
        if (slot := SLOT_RE.fullmatch(mid)) is not None:
            return assign(f"t{slot.group(1)}", slot.group(2))
        m = loaded.get(mid)
        if m is None:
            raise BadRequest(f"Nothing is loaded in slot {mid} on {chosen.name}")
        if dual and m.extruder is None:
            raise BadRequest(
                f"Can't tell which nozzle {m.label} on {chosen.name} feeds",
                "Check the AMS assignment on the printer, or use the preset filament",
            )
        if not m.profile:
            raise BadRequest(f"No slicer preset for {m.kind} on {chosen.name}")
        return m

    def assign(tool_id: str, source: str) -> Material:
        """A filament profile in a changer tool it isn't loaded in yet (the user loads it
        before starting): the tool, holding that profile."""
        tool = loaded.get(tool_id)
        if tool is None or not is_tool(tool):
            raise BadRequest(f"{chosen.name} has no tool {tool_id.upper()}")
        if source == "preset":
            profile = chosen.profiles.filament
        else:
            src = loaded.get(source)
            if src is None or is_tool(src) or not src.profile:
                raise BadRequest("Pick a filament profile to load into the tool")
            profile = src.profile
        if profile == tool.profile and not tool.raw.get("empty"):
            return tool  # already loaded there
        n = int(tool.raw["tool"])
        return Material(
            id=f"{tool_id}.{source}",
            label=f"T{n}: {profile} (load it first)",
            kind=own.filament_types.get(profile, ""),
            colour=own.filament_colours.get(profile),
            profile=profile,
            raw={**tool.raw, "empty": False, "planned": True, "replaces": tool.label},
        )

    profiles = chosen.profiles
    if machine and machine != profiles.printer:
        if machine not in own.printer:
            raise BadRequest(f"No printer profile {machine[:80]!r} for {chosen.name}")
        profiles = replace(profiles, printer=machine)
    if process and process != profiles.process:
        if process not in own.process:
            raise BadRequest(f"No process profile {process[:80]!r} for {chosen.name}")
        profiles = replace(profiles, process=process)
    first: Material | None = None
    if material is not None:
        first = resolve(material)
        profiles = replace(profiles, filament=first.profile)
    extra: list[PartChoice] = []
    if extra_parts:
        if first is None:
            raise BadRequest(
                "Pick a loaded filament for every part of a multi-material print",
                "Choose a slot, not the preset filament",
            )
        part_names = {str(p.get("partId")): str(p.get("name")) for p in onshape.list_parts(req)}
        seen = {req.part_id}
        for part_id, mid in extra_parts:
            if part_id in seen:
                raise BadRequest(f"Part {part_id!r} is listed twice")
            if part_id not in part_names:
                raise BadRequest(f"Part {part_id!r} isn't in this Part Studio")
            seen.add(part_id)
            m = resolve(mid)
            extra.append(PartChoice(part_id, part_names[part_id], m, m.profile))
    tools: tuple[Material, ...] = ()
    chosen_materials = [m for m in (first, *(p.material for p in extra)) if m is not None]
    if any(is_tool(m) for m in chosen_materials):
        if not all(is_tool(m) for m in chosen_materials):
            raise BadRequest(
                "Pick changer tools for every part, or filament profiles for every part",
                "A print can't mix the two",
            )
        if own.mmu_printers and profiles.printer not in own.mmu_printers:
            raise BadRequest(
                f"The printer profile {profiles.printer!r} doesn't use the filament changer",
                f"Pick one that does ({', '.join(own.mmu_printers)}), or a filament profile",
            )
        # Every tool goes to the slicer, in tool order; one nobody prints from gets the
        # printer's filament profile when it has none of its own (an empty gate). A tool
        # the user assigned a filament to holds that one.
        by_tool = {
            int(m.raw["tool"]): m if m.profile else replace(m, profile=chosen.profiles.filament)
            for m in loaded.values()
            if is_tool(m)
        }
        claimed: dict[int, Material] = {}
        for m in chosen_materials:
            n = int(m.raw["tool"])
            if n in claimed and claimed[n].profile != m.profile:
                raise BadRequest(
                    f"T{n} can't hold both {claimed[n].profile} and {m.profile}",
                    "Put the two filaments in different tools",
                )
            claimed[n] = m
            by_tool[n] = m
        tools = tuple(by_tool[n] for n in sorted(by_tool))
    configured_plate = chosen.extra.get("bed_type")
    return PrintPlan(
        req=req,
        document_name=onshape.get_document_name(req.document_id),
        part_name=onshape.get_part_name(req),
        printer=chosen,
        status=status,
        profiles=profiles,
        orientation=orientation,
        settings=settings,
        manual_start=not modules.starts(chosen),
        target_label=target.spec.label,
        material=first,
        bed_type=check_bed_type(bed_type)
        or (configured_plate if isinstance(configured_plate, str) else None)
        or cfg.default_bed_type,
        extra=tuple(extra),
        tools=tools,
    )


def slice_input(
    plan: PrintPlan,
    cfg: Config,
    onshape: OnshapeClient,
    media: Media | None = None,
    *,
    progress: Progress = lambda s: None,
    now: datetime | None = None,
    project: bool = False,
) -> SliceInput:
    """Export and orient the plan's parts (one shared rotation and drop) for a slicer."""
    req = plan.req
    progress("Working out the orientation")
    rotation = rotation_for(plan.orientation, onshape, req, [p.part_id for p in plan.parts])
    n = len(plan.parts)
    progress("Exporting the part from Onshape" if n == 1 else f"Exporting {n} parts from Onshape")
    stls = [
        onshape.export_stl(replace(req, part_id=p.part_id), units="millimeter") for p in plan.parts
    ]
    placed = orient_parts(stls, rotation)
    stem = files.export_path(
        cfg.export_dir, plan.document_name, plan.part_name, req.configuration, "x", now
    ).name.removesuffix(".x")
    auto = plan.orientation.kind == "auto"
    return SliceInput(
        job_name=stem,
        printer=plan.printer,
        parts=tuple(
            PartGeometry(p.name, stl, p.material) for p, stl in zip(plan.parts, placed, strict=True)
        ),
        settings=plan.settings,
        profiles=plan.profiles,
        copies=plan.settings.copies,
        auto_orient=auto,
        auto_arrange=auto,
        bed_type=plan.bed_type,
        media=media,
        extra={"document": plan.document_name, "project": project},
        tools=plan.tools,
    )


def execute_print(
    plan: PrintPlan,
    cfg: Config,
    onshape: OnshapeClient,
    modules: Modules,
    *,
    queue: bool,
    progress: Progress = lambda s: None,
    now: datetime | None = None,
    force_3mf: bool = False,
) -> PrintOutcome:
    """Export → orient → slice → (submit). Call only after explicit confirmation.

    `force_3mf` asks the slicer for a project-style slice (several parts' layout even
    for one part), which "Open in Bambu Studio" needs.
    """
    slicer = modules.slicer_for(plan.printer)
    target = modules.target_for(plan.printer)
    media = media_for(slicer.spec, target.spec)
    job = slice_input(plan, cfg, onshape, media, progress=progress, now=now, project=force_3mf)
    output = slicer.slice(job, progress)
    if not queue:
        return PrintOutcome(job, output, None)
    progress(f"Queueing on {plan.printer.name}")
    submission = target.submit(
        plan.printer,
        output,
        start=not plan.manual_start,
        materials=distinct_materials(job.parts),
        progress=progress,
    )
    return PrintOutcome(job, output, submission)


def studio_project(
    plan: PrintPlan,
    cfg: Config,
    onshape: OnshapeClient,
    modules: Modules,
    progress: Progress = lambda s: None,
) -> bytes:
    """A Bambu Studio project of the plan: its geometry plus the printer, filament and
    process settings the printer's slicer sliced it with (presets, colours, plate,
    walls/infill/supports, prime tower). Slices but never queues. Falls back to
    geometry only (logged) when the slice fails, so the part still opens.
    """
    slicer = modules.slicer_for(plan.printer)
    job = slice_input(plan, cfg, onshape, MEDIA_GCODE_3MF, progress=progress, project=True)
    geometry = bambu_project.build_project(job)
    try:
        if MEDIA_GCODE_3MF not in slicer.spec.makes:
            raise ValueError(f"{slicer.spec.label} doesn't make Bambu projects")
        sliced = slicer.slice(job, progress)
        settings = project_settings_of(sliced.data)
        return with_project_settings(geometry, settings, orca_version_of(sliced.data))
    except (ModuleError, ValueError) as e:
        log.warning("opening %s without slicer settings: %s", plan.part_name, e)
        return geometry
