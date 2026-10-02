"""Extra slicer settings the print panel can offer, chosen on /admin ([panel] in the config).

Each is one OrcaSlicer / Bambu Studio key (or, for the bed temperature, the key the chosen
plate uses). Left empty in the panel, the profile's own value applies; filled in, it goes
on top: process settings into the process profile, filament settings into every filament
of the print (`PrintSettings.extras`). Values are written as profiles store them: strings,
"1"/"0" for booleans.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from os2slice.errors import BadRequest

Scope = Literal["process", "filament"]
Kind = Literal["int", "float", "bool", "choice"]


@dataclass(frozen=True)
class ExtraSetting:
    key: str  # the slicer key, or "bed_temperature" (see BED_KEYS)
    label: str
    scope: Scope
    kind: Kind
    unit: str = ""
    lo: float = 0
    hi: float = 0
    choices: tuple[tuple[str, str], ...] = ()  # (slicer value, label) for kind "choice"
    help: str = ""

    def parse(self, raw: str) -> str:
        """A panel value as the slicer stores it; BadRequest when it doesn't fit."""
        raw = raw.strip()
        if self.kind == "bool":
            if raw not in ("0", "1"):
                raise BadRequest(f"{self.label} must be on or off")
            return raw
        if self.kind == "choice":
            if raw not in {v for v, _ in self.choices}:
                raise BadRequest(f"{self.label}: choose one of the listed values")
            return raw
        try:
            number = int(raw) if self.kind == "int" else float(raw)
        except ValueError:
            kind = "a whole number" if self.kind == "int" else "a number"
            raise BadRequest(f"{self.label} must be {kind}") from None
        if not self.lo <= number <= self.hi:
            raise BadRequest(
                f"{self.label} must be from {number_text(self.lo)} to {number_text(self.hi)}"
            )
        return number_text(number)


def number_text(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


CATALOG: tuple[ExtraSetting, ...] = (
    ExtraSetting(
        "chamber_temperature",
        "Chamber temperature",
        "filament",
        "int",
        "°C",
        0,
        100,
        help="What the printer's start G-code is told (CHAMBER_TEMP)",
    ),
    ExtraSetting("nozzle_temperature", "Nozzle temperature", "filament", "int", "°C", 150, 450),
    ExtraSetting(
        "nozzle_temperature_initial_layer",
        "Nozzle temperature, first layer",
        "filament",
        "int",
        "°C",
        150,
        450,
    ),
    ExtraSetting(
        "bed_temperature",
        "Bed temperature",
        "filament",
        "int",
        "°C",
        0,
        150,
        help="For the chosen build plate, first layer included",
    ),
    ExtraSetting(
        "filament_max_volumetric_speed",
        "Max volumetric speed",
        "filament",
        "float",
        "mm³/s",
        0.5,
        200,
    ),
    ExtraSetting("filament_flow_ratio", "Flow ratio", "filament", "float", "", 0.5, 1.5),
    ExtraSetting("fan_max_speed", "Part fan, max", "filament", "int", "%", 0, 100),
    ExtraSetting("layer_height", "Layer height", "process", "float", "mm", 0.04, 1.0),
    ExtraSetting(
        "initial_layer_print_height", "First layer height", "process", "float", "mm", 0.04, 1.0
    ),
    ExtraSetting(
        "sparse_infill_pattern",
        "Infill pattern",
        "process",
        "choice",
        choices=(
            ("grid", "Grid"),
            ("gyroid", "Gyroid"),
            ("cubic", "Cubic"),
            ("honeycomb", "Honeycomb"),
            ("adaptivecubic", "Adaptive cubic"),
            ("rectilinear", "Rectilinear"),
            ("triangles", "Triangles"),
            ("lightning", "Lightning"),
        ),
    ),
    ExtraSetting(
        "seam_position",
        "Seam position",
        "process",
        "choice",
        choices=(
            ("aligned", "Aligned"),
            ("nearest", "Nearest"),
            ("back", "Back"),
            ("random", "Random"),
        ),
    ),
    ExtraSetting(
        "wall_generator",
        "Wall generator",
        "process",
        "choice",
        choices=(("classic", "Classic"), ("arachne", "Arachne")),
    ),
    ExtraSetting(
        "ironing_type",
        "Ironing",
        "process",
        "choice",
        choices=(
            ("noironing", "None"),
            ("top", "Top surfaces"),
            ("topmost", "Topmost surface"),
            ("solid", "All solid layers"),
        ),
    ),
    ExtraSetting(
        "fuzzy_skin",
        "Fuzzy skin",
        "process",
        "choice",
        choices=(
            ("none", "None"),
            ("external", "Outer walls"),
            ("allwalls", "All walls"),
        ),
    ),
    ExtraSetting("only_one_wall_top", "One wall on top surfaces", "process", "bool"),
    ExtraSetting("enable_prime_tower", "Prime tower", "process", "bool"),
    ExtraSetting("outer_wall_speed", "Outer wall speed", "process", "int", "mm/s", 5, 1000),
    ExtraSetting(
        "default_acceleration", "Default acceleration", "process", "int", "mm/s²", 100, 100000
    ),
)
BY_KEY = {s.key: s for s in CATALOG}

# The bed temperature is one key per build plate (Bambu/Orca names, settings.BED_TYPES).
BED_KEYS = {
    "High Temp Plate": "hot_plate_temp",
    "Smooth PEI Plate": "hot_plate_temp",
    "Textured PEI Plate": "textured_plate_temp",
    "Engineering Plate": "eng_plate_temp",
    "Cool Plate": "cool_plate_temp",
    "Cool Plate (SuperTack)": "supertack_plate_temp",
    "Supertack Plate": "supertack_plate_temp",
}


def check_keys(keys: Iterable[str]) -> tuple[str, ...]:
    """Configured `[panel] extra_settings`, in catalog order; unknown keys are refused."""
    wanted = list(keys)
    unknown = [k for k in wanted if k not in BY_KEY]
    if unknown:
        raise BadRequest(
            f"Unknown extra setting {unknown[0]!r}", f"Use some of: {', '.join(BY_KEY)}"
        )
    return tuple(s.key for s in CATALOG if s.key in wanted)


def parse_form(form: Mapping[str, str], enabled: Iterable[str]) -> tuple[tuple[str, str], ...]:
    """The panel's `x_<key>` fields for the enabled settings; empty ones are left out."""
    out = []
    for key in enabled:
        raw = form.get(f"x_{key}", "")
        if raw.strip():
            out.append((key, BY_KEY[key].parse(raw)))
    return tuple(out)


def overrides(
    extras: Iterable[tuple[str, str]], scope: Scope, bed_type: str | None = None
) -> dict[str, str]:
    """The slicer keys and values of `extras` in one scope. The bed temperature becomes the
    chosen plate's keys (both layers); without a plate it can't be placed and is dropped."""
    out: dict[str, str] = {}
    for key, value in extras:
        setting = BY_KEY.get(key)
        if setting is None or setting.scope != scope:
            continue
        if key == "bed_temperature":
            plate = BED_KEYS.get(bed_type or "")
            if plate:
                out[plate] = value
                out[f"{plate}_initial_layer"] = value
            continue
        out[key] = value
    return out


def describe(extras: Iterable[tuple[str, str]]) -> str:
    """'chamber 50 °C, layer height 0.28 mm' for the job summary."""
    parts = []
    for key, value in extras:
        s = BY_KEY[key]
        shown = dict(s.choices).get(value, value) if s.kind == "choice" else value
        if s.kind == "bool":
            shown = "on" if value == "1" else "off"
        parts.append(f"{s.label.lower()} {shown}{' ' + s.unit if s.unit else ''}")
    return ", ".join(parts)
