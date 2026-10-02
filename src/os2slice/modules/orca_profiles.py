"""OrcaSlicer GUI profiles on disk: the user's own, and the system presets they inherit.

`profile_dir` (a sidecar slicer's field) is either OrcaSlicer's config folder (the one
with `user/` and `system/`, e.g. `~/.config/OrcaSlicer` or the web OrcaSlicer's) or one
user folder (`user/<id>/`). A user folder holds `machine/`, `process/` and `filament/`
JSON files, plus imported preset bundles under `_local/<bundle id>/<kind>/`.

The GUI resolves a profile's `inherits` within the vendor whose system preset it names
(`system/<Vendor>.json` lists each preset's file), so a filament inheriting
"Generic ASA @System" walks OrcaFilamentLibrary's `fdm_filament_asa` while one inheriting
"RatRig Generic ASA" walks RatRig's. The sidecar looks parents up in one folder only, so
`flatten` does the walk here and uploads the result whole (docs/SLICERAPI_API.md).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from os2slice.modules.base import ModuleError

KINDS = ("machine", "process", "filament")
# The vendor index lists, by the kind of preset they hold.
INDEX_LISTS = {
    "machine": ("machine_list",),
    "process": ("process_list",),
    "filament": ("filament_list",),
}
# Vendors whose presets keep settings in companion files the sidecar knows how to merge
# ("<preset> template <key>.json"); their profiles keep `inherits` for it to resolve.
SIDECAR_VENDORS = frozenset({"BBL"})
# Old key -> new key, from PrintConfigDef::handle_legacy (OrcaSlicer 2.4.2), for the renames
# that keep the value as it is. The GUI renames them in each file as it loads it, so a
# user's `chamber_temperature` overrides a base preset's old `chamber_temperatures`. In a
# merged file the CLI would rename too late and the old key could win.
LEGACY_KEYS = {
    "bridge_fan_speed": "overhang_fan_speed",
    "chamber_temperatures": "chamber_temperature",
    "cooling": "slow_down_for_layer_cooling",
    "counterbole_hole_bridging": "counterbore_hole_bridging",
    "enable_wipe_tower": "enable_prime_tower",
    "extruder_clearance_max_radius": "extruder_clearance_radius",
    "initial_layer_flow_ratio": "bottom_solid_infill_flow_ratio",
    "machine_switch_extruder_time": "machine_tool_change_time",
    "prime_tower_extra_rib_length": "wipe_tower_extra_rib_length",
    "prime_tower_fillet_wall": "wipe_tower_fillet_wall",
    "prime_tower_rib_width": "wipe_tower_rib_width",
    "sparse_infill_anchor": "infill_anchor",
    "sparse_infill_anchor_max": "infill_anchor_max",
    "support_material_angle": "support_angle",
    "support_material_enforce_layers": "enforce_support_layers",
    "support_material_extruder": "support_filament",
    "support_material_interface_extruder": "support_interface_filament",
    "tool_change_gcode": "change_filament_gcode",
    "wipe_tower_brim_width": "prime_tower_brim_width",
    "wipe_tower_extruder": "wipe_tower_filament",
    "wipe_tower_width": "prime_tower_width",
    "wiping_volume": "prime_volume",
}
MAX_DEPTH = 16
# OrcaSlicer's complete default filament. The GUI fills every key a preset chain leaves out
# from its built-in defaults; some vendors' base presets (RatRig's) leave out keys the CLI
# needs one of per filament ("filament_is_support's count 1 not equal to filament_colour's
# size 2", exit 251, with two filaments). Gaps are filled from this one, never values.
FILAMENT_DEFAULTS = ("OrcaFilamentLibrary", "fdm_filament_common")
IDENTITY_KEYS = frozenset({
    "name", "inherits", "from", "type", "instantiation", "setting_id", "filament_id",
    "compatible_printers", "compatible_printers_condition", "version",
})  # fmt: skip


@dataclass
class OrcaProfiles:
    """The user profiles by kind and name, and the system presets by vendor."""

    users: dict[str, dict[str, dict[str, Any]]] = field(
        default_factory=lambda: {k: {} for k in KINDS}
    )
    # (vendor, kind) -> preset name -> file
    system: dict[tuple[str, str], dict[str, Path]] = field(default_factory=dict)

    @classmethod
    def load(cls, root: Path | None, where: str = "profile_dir") -> OrcaProfiles:
        found = cls()
        if root is None or not root.is_dir():
            return found
        if (root / "user").is_dir() or (root / "system").is_dir():
            config = root
            folders = sorted(
                (p for p in (root / "user").glob("*") if p.is_dir()),
                key=lambda p: (p.name == "default", p.name),  # the signed-in user first
            )
        else:
            config = root.parent.parent  # <config>/user/<id>
            folders = [root]
        for folder in folders:
            for kind in KINDS:
                own = sorted((folder / kind).glob("*.json"))
                bundled = sorted(folder.glob(f"_local/*/{kind}/*.json"))
                for path in own + bundled:  # a name of your own wins over a bundle's
                    body = _read(path, where)
                    if isinstance(body, dict):
                        found.users[kind].setdefault(str(body.get("name") or path.stem), body)
        found.system = _system_index(config / "system", where)
        return found

    def names(self, kind: str) -> tuple[str, ...]:
        return tuple(sorted(self.users[kind], key=str.casefold))

    def flatten(self, body: dict[str, Any], kind: str) -> dict[str, Any]:
        """`body` with its system parents merged in, as the GUI loads it.

        Left as it is when its parent isn't in the system index (the sidecar then looks
        for it in its own folder) or belongs to a vendor the sidecar resolves better.
        """
        parent = body.get("inherits")
        if not isinstance(parent, str) or not parent:
            return dict(body)
        vendor = self._vendor_of(parent, kind)
        if vendor is None or vendor in SIDECAR_VENDORS:
            return dict(body)
        index = self.system[(vendor, kind)]
        merged = _renamed(body)
        seen: set[str] = set()
        while isinstance(parent, str) and parent:
            if parent in seen or len(seen) >= MAX_DEPTH:
                raise ModuleError(f"The {kind} profile {body.get('name')!r} inherits in a loop")
            seen.add(parent)
            path = index.get(parent)
            if path is None:
                break  # as the GUI does with a dangling parent: keep what we have
            base = _read(path, f"system/{vendor}")
            if not isinstance(base, dict):
                break
            merged = {**_renamed(base), **merged}
            parent = base.get("inherits")
        merged.pop("inherits", None)
        if kind == "filament":
            for key, value in self._filament_defaults().items():
                merged.setdefault(key, value)
        # Resolved, it is in effect a system preset; the CLI refuses "User" and "Bundle"
        # (the sidecar marks its own resolved profiles the same way).
        merged.update({"type": kind, "from": "system"})
        return merged

    def _filament_defaults(self) -> dict[str, Any]:
        vendor, name = FILAMENT_DEFAULTS
        path = self.system.get((vendor, "filament"), {}).get(name)
        base = _read(path, f"system/{vendor}") if path is not None else None
        if not isinstance(base, dict):
            return {}
        return {k: v for k, v in _renamed(base).items() if k not in IDENTITY_KEYS}

    def _vendor_of(self, name: str, kind: str) -> str | None:
        for (vendor, k), index in sorted(self.system.items()):
            if k == kind and name in index:
                return vendor
        return None


def _renamed(body: dict[str, Any]) -> dict[str, Any]:
    """`body` with legacy keys under their current names (a current key wins)."""
    out = {k: v for k, v in body.items() if k not in LEGACY_KEYS}
    for old, new in LEGACY_KEYS.items():
        if old in body:
            out.setdefault(new, body[old])
    return out


def _system_index(system: Path, where: str) -> dict[tuple[str, str], dict[str, Path]]:
    out: dict[tuple[str, str], dict[str, Path]] = {}
    if not system.is_dir():
        return out
    for meta_path in sorted(system.glob("*.json")):
        vendor = meta_path.stem
        meta = _read(meta_path, where)
        if not isinstance(meta, dict):
            continue
        for kind, lists in INDEX_LISTS.items():
            index: dict[str, Path] = {}
            for key in lists:
                for entry in meta.get(key) or []:
                    if not isinstance(entry, dict):
                        continue
                    name, sub = entry.get("name"), entry.get("sub_path")
                    if not (isinstance(name, str) and isinstance(sub, str)):
                        continue
                    path = (system / vendor / sub).resolve()
                    if path.is_relative_to(system.resolve()):  # never outside system/
                        index.setdefault(name, path)
            if index:
                out[(vendor, kind)] = index
    return out


def _read(path: Path, where: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ModuleError(
            f"Can't read the profile {path.name}: {e}", f"Fix or remove it ({where})"
        ) from e
