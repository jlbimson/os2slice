from __future__ import annotations

import math
import struct

import pytest

from os2slice.errors import BadRequest
from os2slice.orientation import (
    AXES,
    IDENTITY,
    Orientation,
    bounding_box,
    check_rotation,
    face_down,
    orient_stl,
    rotation_to,
)

# 50.8 x 25.4 x 6.35 mm block, bottom at z=0 (like the Onshape test part).
SIZE = (50.8, 25.4, 6.35)


def block_stl(sx: float, sy: float, sz: float) -> bytes:
    v = [(x, y, z) for x in (0, sx) for y in (0, sy) for z in (0, sz)]
    faces = [(0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5), (0, 4, 5), (0, 5, 1),
             (2, 3, 7), (2, 7, 6), (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3)]  # fmt: skip
    out = b"\0" * 80 + struct.pack("<I", len(faces))
    for f in faces:
        out += struct.pack("<3f", 0, 0, 0)
        for i in f:
            out += struct.pack("<3f", *v[i])
        out += b"\0\0"
    return out


def apply(m, v):
    return tuple(sum(m[r][c] * v[c] for c in range(3)) for r in range(3))


@pytest.mark.parametrize("axis", sorted(AXES))
def test_face_down_sends_that_axis_to_minus_z(axis: str) -> None:
    m = face_down(AXES[axis])
    check_rotation(m)
    assert apply(m, AXES[axis]) == pytest.approx((0, 0, -1), abs=1e-9)


def test_arbitrary_normal() -> None:
    n = (0.3, -0.5, 0.81)
    m = face_down(n)
    check_rotation(m)
    k = math.sqrt(sum(x * x for x in n))
    assert apply(m, tuple(x / k for x in n)) == pytest.approx((0, 0, -1), abs=1e-9)


def test_rotation_to_identity() -> None:
    assert rotation_to((0, 0, -1)) == IDENTITY


def size_of(data: bytes) -> tuple[float, float, float]:
    lo, hi = bounding_box(data)
    return tuple(round(h - lo_, 3) for lo_, h in zip(lo, hi, strict=True))  # type: ignore[return-value]


def test_orient_as_modeled_keeps_geometry() -> None:
    data = block_stl(*SIZE)
    out = orient_stl(data, IDENTITY)
    assert size_of(out) == pytest.approx(SIZE, abs=1e-3)
    assert len(out) == len(data)


@pytest.mark.parametrize(
    ("axis", "expected_height"),
    [("-z", 6.35), ("+z", 6.35), ("+x", 50.8), ("-x", 50.8), ("+y", 25.4), ("-y", 25.4)],
)
def test_orient_puts_the_side_down_and_on_the_bed(axis: str, expected_height: float) -> None:
    out = orient_stl(block_stl(*SIZE), face_down(AXES[axis]))
    lo, hi = bounding_box(out)
    assert lo[2] == pytest.approx(0.0, abs=1e-4)
    assert hi[2] - lo[2] == pytest.approx(expected_height, abs=1e-3)
    assert int.from_bytes(out[80:84], "little") == 12


def test_orient_rejects_mirrors_and_junk() -> None:
    mirror = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, -1.0))
    with pytest.raises(BadRequest, match="mirror"):
        orient_stl(block_stl(*SIZE), mirror)
    with pytest.raises(BadRequest, match="orthonormal"):
        check_rotation(((2.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)))
    with pytest.raises(BadRequest, match="non-finite"):
        check_rotation(((math.nan, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)))
    with pytest.raises(BadRequest, match="binary STL"):
        orient_stl(b"solid x\n", IDENTITY)


def test_zero_normal() -> None:
    with pytest.raises(BadRequest):
        face_down((0.0, 0.0, 0.0))


@pytest.mark.parametrize(
    ("text", "kind", "value"),
    [
        ("", "axis", "-z"),
        ("as-modeled", "axis", "-z"),
        ("x+", "axis", "+x"),
        ("Z-", "axis", "-z"),
        ("-y", "axis", "-y"),
        ("auto", "auto", "auto"),
        ("face:JHG", "face", "JHG"),
    ],
)
def test_orientation_parse(text: str, kind: str, value: str) -> None:
    o = Orientation.parse(text)
    assert (o.kind, o.value) == (kind, value)


@pytest.mark.parametrize("text", ["sideways", "face:", "face:../x", "face:" + "A" * 33, "xx+"])
def test_orientation_parse_rejects(text: str) -> None:
    with pytest.raises(BadRequest):
        Orientation.parse(text)


def test_orient_parts_keeps_relative_positions() -> None:
    from os2slice.orientation import orient_parts

    base = block_stl(60, 30, 4)
    # "Text" sits on top of the base: shift a small block up by 4 mm.
    text = bytearray(block_stl(40, 10, 1))
    for i in range(12):
        for v in range(3):
            off = 84 + 50 * i + 12 + 12 * v + 8
            z = struct.unpack_from("<f", text, off)[0]
            struct.pack_into("<f", text, off, z + 4.0)
    out = orient_parts([base, bytes(text)], face_down(AXES["+z"]))  # flip upside down
    (blo, bhi), (tlo, _) = bounding_box(out[0]), bounding_box(out[1])
    assert tlo[2] == pytest.approx(0.0, abs=1e-4)  # the text is now at the bottom
    assert blo[2] == pytest.approx(1.0, abs=1e-4)  # the base sits on the text
    assert bhi[2] == pytest.approx(5.0, abs=1e-4)
    single = orient_stl(base, face_down(AXES["+z"]))
    assert bounding_box(single)[0][2] == pytest.approx(0.0, abs=1e-4)
