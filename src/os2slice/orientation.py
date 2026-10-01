"""Orient a binary STL for printing: rotate it, then drop it onto Z = 0 (D-14).

Pure stdlib. A rotation is a 3x3 row-major matrix that must be a proper
rotation (orthonormal, determinant +1), so a mirrored part can't slip through.
"""

from __future__ import annotations

import math
import re
import struct
from array import array
from dataclasses import dataclass
from typing import Literal

from os2slice.errors import BadRequest

Matrix = tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]
Vec = tuple[float, float, float]

IDENTITY: Matrix = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
DOWN: Vec = (0.0, 0.0, -1.0)
AXES: dict[str, Vec] = {
    "+x": (1.0, 0.0, 0.0),
    "-x": (-1.0, 0.0, 0.0),
    "+y": (0.0, 1.0, 0.0),
    "-y": (0.0, -1.0, 0.0),
    "+z": (0.0, 0.0, 1.0),
    "-z": (0.0, 0.0, -1.0),
}
FACE_ID_RE = re.compile(r"[A-Za-z0-9_+\-]{1,32}")
TOL = 1e-6


@dataclass(frozen=True)
class Orientation:
    """What the user asked for. `face` needs its normal looked up in Onshape."""

    kind: Literal["axis", "face", "auto"]
    value: str = "-z"

    @classmethod
    def parse(cls, text: str) -> Orientation:
        """'as-modeled' (= '-z'), '+x' or 'x+' … (that side down), 'face:<id>', or 'auto'."""
        t = (text or "-z").strip()
        if t in ("auto",):
            return cls("auto", "auto")
        if t in ("as-modeled", "modeled"):
            return cls("axis", "-z")
        low = t.lower()
        if len(low) == 2 and low[0] in "xyz" and low[1] in "+-":
            low = low[1] + low[0]  # "z-" (CLI-friendly) == "-z"
        if low in AXES:
            return cls("axis", low)
        if t.startswith("face:") and FACE_ID_RE.fullmatch(t[5:]):
            return cls("face", t[5:])
        raise BadRequest("Orientation must be -z (as modeled), +x/-x/+y/-y/+z, face:<id> or auto")

    def describe(self) -> str:
        if self.kind == "auto":
            return "slicer auto-orient"
        if self.kind == "face":
            return f"face {self.value} down"
        return "as modeled" if self.value == "-z" else f"{self.value} side down"


def rotation_to(src: Vec, dst: Vec = DOWN) -> Matrix:
    """The rotation taking unit vector `src` onto `dst` (Rodrigues)."""
    a, b = _unit(src), _unit(dst)
    c = _dot(a, b)
    if c > 1 - TOL:
        return IDENTITY
    if c < -1 + TOL:
        # 180°: rotate about any axis perpendicular to a.
        helper = (1.0, 0.0, 0.0) if abs(a[0]) < 0.9 else (0.0, 1.0, 0.0)
        k = _unit(_cross(a, helper))
        return _axis_angle(k, math.pi)
    k = _cross(a, b)
    s = math.sqrt(_dot(k, k))
    return _axis_angle((k[0] / s, k[1] / s, k[2] / s), math.atan2(s, c))


def face_down(normal: Vec) -> Matrix:
    """Rotation that puts a face with outward `normal` flat on the bed."""
    return rotation_to(normal, DOWN)


def check_rotation(m: Matrix) -> None:
    rows = [tuple(float(x) for x in r) for r in m]
    if len(rows) != 3 or any(len(r) != 3 for r in rows):
        raise BadRequest("Rotation must be 3x3")
    if not all(math.isfinite(x) for r in rows for x in r):
        raise BadRequest("Rotation has non-finite values")
    for i in range(3):
        for j in range(3):
            want = 1.0 if i == j else 0.0
            if abs(_dot(rows[i], rows[j]) - want) > 1e-6:  # type: ignore[arg-type]
                raise BadRequest("Rotation isn't orthonormal")
    if abs(_det(m) - 1.0) > 1e-6:
        raise BadRequest("Rotation would mirror the part")


_REC = struct.Struct("<12fH")


def _rotate(data: bytes, m: Matrix) -> tuple[array, list[int], float]:
    """Rotated facet floats (normal + 3 vertices per facet), attributes, and min Z."""
    count = int.from_bytes(data[80:84], "little")
    if len(data) != 84 + 50 * count or count == 0:
        raise BadRequest("Not a non-empty binary STL")
    floats = array("f")
    attrs: list[int] = []
    for i in range(count):
        *vals, attr = _REC.unpack_from(data, 84 + 50 * i)
        floats.extend(vals)
        attrs.append(attr)
    (a, b, c), (d, e, f), (g, h, k) = m
    min_z = math.inf
    for base in range(0, len(floats), 3):
        x, y, z = floats[base], floats[base + 1], floats[base + 2]
        floats[base] = a * x + b * y + c * z
        floats[base + 1] = d * x + e * y + f * z
        floats[base + 2] = g * x + h * y + k * z
        if base % 12:  # skip facet normals (every 4th triple) for min Z
            min_z = min(min_z, floats[base + 2])
    return floats, attrs, min_z


def _pack(header: bytes, floats: array, attrs: list[int], dz: float) -> bytes:
    out = bytearray(header[:84])
    for i, attr in enumerate(attrs):
        vals = floats[i * 12 : i * 12 + 12]
        for v in range(3, 12, 3):
            vals[v + 2] -= dz
        out += _REC.pack(*vals, attr)
    return bytes(out)


def orient_stl(data: bytes, m: Matrix) -> bytes:
    """Rotate every facet of a binary STL by `m` and translate so min Z = 0."""
    check_rotation(m)
    floats, attrs, min_z = _rotate(data, m)
    return _pack(data, floats, attrs, min_z)


def orient_parts(parts: list[bytes], m: Matrix) -> list[bytes]:
    """Rotate several STLs together and drop them onto Z = 0 as one assembly.

    They keep their relative positions (text stays on its base), because every
    part gets the same rotation and the same translation.
    """
    check_rotation(m)
    rotated = [_rotate(p, m) for p in parts]
    floor = min(r[2] for r in rotated)
    return [_pack(p, fl, at, floor) for p, (fl, at, _) in zip(parts, rotated, strict=True)]


def translate_xy(data: bytes, dx: float, dy: float) -> bytes:
    """Move a binary STL in X and Y (Z unchanged)."""
    out = bytearray(data)
    count = int.from_bytes(data[80:84], "little")
    for i in range(count):
        for v in range(3):
            off = 84 + 50 * i + 12 + 12 * v
            x, y = struct.unpack_from("<2f", out, off)
            struct.pack_into("<2f", out, off, x + dx, y + dy)
    return bytes(out)


def bounding_box(data: bytes) -> tuple[Vec, Vec]:
    count = int.from_bytes(data[80:84], "little")
    lo = [math.inf] * 3
    hi = [-math.inf] * 3
    for i in range(count):
        verts = struct.unpack_from("<9f", data, 84 + 50 * i + 12)
        for v in range(0, 9, 3):
            for ax in range(3):
                lo[ax] = min(lo[ax], verts[v + ax])
                hi[ax] = max(hi[ax], verts[v + ax])
    return (lo[0], lo[1], lo[2]), (hi[0], hi[1], hi[2])


def _axis_angle(k: Vec, theta: float) -> Matrix:
    x, y, z = k
    c, s = math.cos(theta), math.sin(theta)
    t = 1 - c
    return (
        (t * x * x + c, t * x * y - s * z, t * x * z + s * y),
        (t * x * y + s * z, t * y * y + c, t * y * z - s * x),
        (t * x * z - s * y, t * y * z + s * x, t * z * z + c),
    )


def _unit(v: Vec) -> Vec:
    n = math.sqrt(_dot(v, v))
    if not math.isfinite(n) or n < 1e-9:
        raise BadRequest("Face normal is zero or invalid")
    return (v[0] / n, v[1] / n, v[2] / n)


def _dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: Vec, b: Vec) -> Vec:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _det(m: Matrix) -> float:
    (a, b, c), (d, e, f), (g, h, k) = m
    return a * (e * k - f * h) - b * (d * k - f * g) + c * (d * h - e * g)
