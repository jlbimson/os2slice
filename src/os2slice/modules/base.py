"""The module contract: slicers turn geometry into a print file, targets accept it.

This file is the shared interface between the core (printing, server, CLI) and the
pluggable modules under ``os2slice.modules``. Keep it dependency-free beyond the
standard library and ``os2slice.settings`` / ``os2slice.errors``; modules import
from here, never the other way round. See docs/MODULES.md for the design and D-27.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal, Protocol, runtime_checkable

from os2slice.errors import Os2sliceError
from os2slice.settings import PrintSettings

Technology = Literal["fdm", "sla"]
Progress = Callable[[str], None]

# Media of a sliced print file. A target lists what it accepts; a slicer what it makes.
MEDIA_GCODE_3MF = "gcode.3mf"  # Bambu / Orca sliced project (ZIP with Metadata/plate_1.gcode)
MEDIA_GCODE = "gcode"  # plain G-code
MEDIA_BGCODE = "bgcode"  # Prusa binary G-code
MEDIA_FORM = "form"  # Formlabs PreForm job
Media = Literal["gcode.3mf", "gcode", "bgcode", "form"]


class ModuleError(Os2sliceError):
    """A module couldn't do its job (network, refused request, bad output)."""

    exit_code = 6
    http_status = 502


# ---------------------------------------------------------------------------
# Config schema: what each module kind needs, so the config page can render a form
# and config.py can validate without knowing the module.

FieldType = Literal["str", "int", "bool", "url", "path", "secret", "list", "choice"]


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    type: FieldType = "str"
    required: bool = False
    default: Any = None
    help: str = ""
    choices: tuple[str, ...] = ()  # for type "choice"
    # Secrets are never stored in config.toml. They live in the secret store under
    # "<section>.<name>.<key>" (e.g. "targets.farm.api_key") and are never echoed back.


@dataclass(frozen=True)
class ModuleSpec:
    kind: str  # the `kind = "..."` value in config
    label: str  # shown in the config page
    role: Literal["slicer", "target", "both"]
    technology: Technology
    fields: tuple[Field, ...]
    makes: tuple[Media, ...] = ()  # slicer: output media it can produce
    accepts: tuple[Media, ...] = ()  # target: media it can take
    discovers_printers: bool = False  # target: printers() comes from the service, not config
    pairs_only_with: tuple[str, ...] = ()  # kinds this module must be paired with (PreForm)
    help: str = ""


# ---------------------------------------------------------------------------
# Shared data.


@dataclass(frozen=True)
class Profiles:
    """Slicer profile names for one printer (what BamBuddy called presets)."""

    printer: str = ""
    process: str = ""
    filament: str = ""  # default material profile; per-part choices override it


@dataclass(frozen=True)
class Material:
    """Something loaded in or chosen for a printer: an AMS tray, a spool, a resin."""

    id: str  # module-specific, stable within the printer (global tray id, spool id, SKU)
    label: str  # "AMS 1 slot 2: PETG, black"
    kind: str = ""  # "PETG", "FLGPBK05"
    colour: str | None = None  # "#RRGGBB"
    extruder: int | None = None  # which nozzle feeds it, on dual-nozzle printers
    profile: str = ""  # the slicer profile that matches it, when known
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PrinterInfo:
    """A printer the UI can offer. Either configured ([printers.<key>]) or discovered."""

    key: str  # unique across the config: the [printers.<key>] key, or "<target>/<id>"
    name: str
    technology: Technology
    model: str
    target: str  # [targets.<key>]
    slicer: str  # [slicers.<key>]
    profiles: Profiles = Profiles()
    bed_mm: tuple[float, float] | None = None  # printable area, for layout and the preview
    nozzle_count: int = 1
    active: bool = True
    ui_url: str = ""  # where a person watches it (BamBuddy queue, Mainsail, PrusaLink…)
    extra: Mapping[str, Any] = field(default_factory=dict)  # module-specific (bed_type, ip…)


@dataclass(frozen=True)
class PrinterStatus:
    state: str  # the target's own word: "IDLE", "RUNNING", "PRINTING", "offline"…
    connected: bool
    ready: bool  # may take a new job now
    detail: str = ""  # "waiting for the plate to be cleared"
    materials: tuple[Material, ...] = ()  # what's loaded (empty when the target can't tell)
    raw: Mapping[str, Any] = field(default_factory=dict)

    def describe(self) -> str:
        return f"{self.state}, {self.detail}" if self.detail else self.state


@dataclass(frozen=True)
class PartGeometry:
    """One part's oriented mesh, with the material it prints in."""

    name: str
    stl: bytes  # binary STL, millimetres, already rotated and sitting at Z = 0
    material: Material | None = None  # None = the default filament profile


@dataclass(frozen=True)
class SliceInput:
    """Everything a slicer needs. The core builds it; modules never read config directly."""

    job_name: str  # safe for filenames (files.py sanitised it)
    printer: PrinterInfo
    parts: tuple[PartGeometry, ...]  # one, or several printed as one object (D-20)
    settings: PrintSettings
    profiles: Profiles  # resolved for this print (filament may differ from printer default)
    copies: int = 1
    auto_orient: bool = False  # the user asked the slicer to orient (else parts are final)
    auto_arrange: bool = False
    bed_type: str | None = None  # Bambu plate name, when the module understands it
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SliceOutput:
    data: bytes
    filename: str  # "<job_name>.gcode.3mf", "<job_name>.gcode", "<job_name>.form"
    media: Media
    print_time_s: int | None = None
    material_g: float | None = None
    layers: int | None = None
    report: Mapping[str, Any] = field(default_factory=dict)  # module-specific facts, logged
    warnings: tuple[str, ...] = ()  # shown before Print (unsupported minima, thin walls…)


@dataclass(frozen=True)
class Submission:
    """What happened after a target took the file."""

    id: str  # queue item id, filename on the printer, PreForm job id
    state: Literal["waiting", "started"]  # waiting = a person must start it (D-13)
    detail: str = ""  # "item 42, waits for Start in BamBuddy"
    url: str = ""  # where to watch it
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Health:
    ok: bool
    summary: str  # one line for `doctor` and the config page
    version: str = ""
    detail: str = ""


@dataclass(frozen=True)
class ProfileCatalog:
    printer: tuple[str, ...] = ()
    process: tuple[str, ...] = ()
    filament: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# The protocols. A module class carries `spec` and is built by its factory from
# the validated field values (secrets already resolved) plus an httpx transport
# override for tests.


@runtime_checkable
class Slicer(Protocol):
    spec: ClassVar[ModuleSpec]

    def check(self) -> Health: ...

    def profiles(self, printer_model: str = "") -> ProfileCatalog:
        """Profile names this slicer knows, optionally narrowed to a printer model."""
        ...

    def slice(self, job: SliceInput, progress: Progress) -> SliceOutput:
        """Blocking; may take minutes. Raise ModuleError with the slicer's own reason."""
        ...


@runtime_checkable
class Target(Protocol):
    spec: ClassVar[ModuleSpec]

    def check(self) -> Health: ...

    def printers(self, configured: tuple[PrinterInfo, ...]) -> tuple[PrinterInfo, ...]:
        """The printers this target offers.

        Discovering targets (BamBuddy, PreForm) return what they find, merged with
        the matching configured entries (profiles, bed type) by name. Others return
        `configured` as is, possibly filling model/bed from the device.
        """
        ...

    def status(self, printer: PrinterInfo) -> PrinterStatus: ...

    def submit(
        self,
        printer: PrinterInfo,
        output: SliceOutput,
        *,
        start: bool,
        materials: tuple[Material, ...] = (),
        progress: Progress = lambda s: None,
    ) -> Submission:
        """Hand the file to the printer or queue. `start=False` must leave it waiting
        for a person (D-13); a target that can't do that raises ModuleError."""
        ...


Factory = Callable[..., Any]  # factory(values: Mapping[str, Any], *, transport=None) -> module
