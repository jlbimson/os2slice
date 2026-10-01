"""Validated print settings and their BamBuddy/Bambu Studio process overrides (D-14)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

from os2slice.errors import BadRequest

Supports = Literal["off", "normal", "tree"]
SUPPORTS: tuple[Supports, ...] = ("off", "normal", "tree")
SUPPORT_TYPE = {"normal": "normal(auto)", "tree": "tree(auto)"}

# BamBuddy's canonical build plate names (SliceRequest.bed_type), docs/BAMBUDDY_API.md.
BED_TYPES = (
    "Textured PEI Plate",
    "Smooth PEI Plate",
    "High Temp Plate",
    "Engineering Plate",
    "Cool Plate",
    "Cool Plate (SuperTack)",
    "Supertack Plate",
)


# What the plate menus show. Bambu Studio writes the smooth PEI plate as "High Temp Plate"
# (Josh's own Carriage print has curr_bed_type 'High Temp Plate'); the separate
# "Smooth PEI Plate" value slices differently (it refused PETG), so it's only an alias.
PLATE_LABELS = {
    "High Temp Plate": "Smooth PEI (High Temp Plate)",
    "Textured PEI Plate": "Textured PEI",
    "Engineering Plate": "Engineering",
    "Cool Plate": "Cool Plate",
    "Cool Plate (SuperTack)": "Cool Plate SuperTack",
    "Supertack Plate": "SuperTack",
}
PLATE_ALIASES = {"Smooth PEI Plate": "High Temp Plate"}


def check_bed_type(value: str | None) -> str | None:
    """'' / None → None (preset default); otherwise one of BED_TYPES ("Smooth PEI" aliases)."""
    if not value:
        return None
    value = PLATE_ALIASES.get(value, value)
    if value not in BED_TYPES:
        raise BadRequest(
            f"Unknown build plate {value[:40]!r}", f"Use one of: {', '.join(BED_TYPES)}"
        )
    return value


WALLS_RANGE = (1, 10)
INFILL_RANGE = (0, 100)


@dataclass(frozen=True)
class PrintSettings:
    walls: int = 2
    infill: int = 15  # percent
    supports: Supports = "off"
    build_plate_only: bool = False

    def __post_init__(self) -> None:
        _check_int("walls", self.walls, WALLS_RANGE)
        _check_int("infill", self.infill, INFILL_RANGE)
        if self.supports not in SUPPORTS:
            raise BadRequest(f"supports must be one of {', '.join(SUPPORTS)}")
        if not isinstance(self.build_plate_only, bool):
            raise BadRequest("build_plate_only must be true or false")

    def process_overrides(self) -> dict[str, Any]:
        """Bambu Studio process keys, in the shapes verified in docs/BAMBUDDY_API.md."""
        overrides: dict[str, Any] = {
            "wall_loops": self.walls,
            "sparse_infill_density": f"{self.infill}%",
            "enable_support": 1 if self.supports != "off" else 0,
        }
        if self.supports != "off":
            overrides["support_type"] = SUPPORT_TYPE[self.supports]
            overrides["support_on_build_plate_only"] = 1 if self.build_plate_only else 0
        return overrides

    def describe(self) -> str:
        sup = "no supports" if self.supports == "off" else f"{self.supports} supports"
        if self.supports != "off" and self.build_plate_only:
            sup += " (build plate only)"
        return f"{self.walls} walls, {self.infill}% infill, {sup}"

    @classmethod
    def from_strings(cls, values: Mapping[str, str], defaults: PrintSettings) -> PrintSettings:
        """Build from untrusted form/CLI strings; missing keys keep `defaults`."""
        walls = _parse_int("walls", values.get("walls"), defaults.walls)
        infill = _parse_int("infill", values.get("infill"), defaults.infill)
        supports = values.get("supports") or defaults.supports
        if supports not in SUPPORTS:
            raise BadRequest(f"supports must be one of {', '.join(SUPPORTS)}")
        bpo_raw = values.get("build_plate_only")
        if bpo_raw is None:
            bpo = defaults.build_plate_only
        elif bpo_raw in ("1", "true", "on", "yes"):
            bpo = True
        elif bpo_raw in ("0", "false", "off", "no", ""):
            bpo = False
        else:
            raise BadRequest("build_plate_only must be true or false")
        return cls(walls, infill, cast(Supports, supports), bpo)


def _check_int(name: str, value: object, bounds: tuple[int, int]) -> None:
    lo, hi = bounds
    if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
        raise BadRequest(f"{name} must be a whole number from {lo} to {hi}")


def _parse_int(name: str, raw: str | None, default: int) -> int:
    if raw is None or raw == "":
        return default
    raw = raw.strip().removesuffix("%")
    if not raw.isdigit() or len(raw) > 3:
        raise BadRequest(f"{name} must be a whole number")
    return int(raw)
