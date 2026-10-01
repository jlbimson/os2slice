"""Build a Bambu-style 3MF: one printable object made of several parts, each with its own filament.

Layout copied from what Bambu Studio 02.08 writes (docs/BAMBUDDY_API.md): the
meshes live in 3D/Objects/object_1.model, 3D/3dmodel.model assembles them as
components of one object, and Metadata/model_settings.config gives each part its
filament ("extruder", 1-based filament slot). Pure stdlib.
"""

from __future__ import annotations

import io
import json
import re
import struct
import uuid
import zipfile
from dataclasses import dataclass
from html import escape

CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
PROD = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
BBL = "http://schemas.bambulab.com/package/2021"
MODEL_REL = "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"


@dataclass(frozen=True)
class Part:
    name: str
    stl: bytes  # binary STL, already oriented and placed (shared coordinate frame)
    filament: int  # 1-based filament slot


def _mesh(stl: bytes) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]:
    count = int.from_bytes(stl[80:84], "little")
    index: dict[tuple[float, float, float], int] = {}
    verts: list[tuple[float, float, float]] = []
    tris: list[tuple[int, int, int]] = []
    for i in range(count):
        v = struct.unpack_from("<9f", stl, 84 + 50 * i + 12)
        tri = []
        for k in range(0, 9, 3):
            key = (round(v[k], 5), round(v[k + 1], 5), round(v[k + 2], 5))
            if key not in index:
                index[key] = len(verts)
                verts.append(key)
            tri.append(index[key])
        if len(set(tri)) == 3:  # drop degenerate facets
            tris.append((tri[0], tri[1], tri[2]))
    return verts, tris


def _header(extra: str = "") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<model unit="millimeter" xml:lang="en-US" xmlns="{CORE}" xmlns:BambuStudio="{BBL}" '
        f'xmlns:p="{PROD}" requiredextensions="p">\n'
        ' <metadata name="BambuStudio:3mfVersion">1</metadata>\n' + extra
    )


def _plate(object_id: int, filament_maps: list[int] | None) -> str:
    if not filament_maps:
        return ""
    maps = " ".join(str(int(m)) for m in filament_maps)
    volume = " ".join("0" for _ in filament_maps)
    return (
        "  <plate>\n"
        '    <metadata key="plater_id" value="1"/>\n'
        '    <metadata key="plater_name" value=""/>\n'
        '    <metadata key="locked" value="false"/>\n'
        '    <metadata key="filament_map_mode" value="Manual"/>\n'
        f'    <metadata key="filament_maps" value="{maps}"/>\n'
        f'    <metadata key="filament_volume_maps" value="{volume}"/>\n'
        "    <model_instance>\n"
        f'      <metadata key="object_id" value="{object_id}"/>\n'
        '      <metadata key="instance_id" value="0"/>\n'
        '      <metadata key="identify_id" value="1"/>\n'
        "    </model_instance>\n"
        "  </plate>\n"
    )


def build_3mf(parts: list[Part], object_name: str, filament_maps: list[int] | None = None) -> bytes:
    """One object whose components are `parts`, each assigned its filament slot.

    `filament_maps` (dual-nozzle printers) pins each filament to a slicer extruder
    (1-based, one entry per filament) via the plate's Manual filament map.
    """
    if not parts:
        raise ValueError("no parts")
    objects = []
    for n, part in enumerate(parts, start=1):
        verts, tris = _mesh(part.stl)
        vx = "".join(f'<vertex x="{x:.6g}" y="{y:.6g}" z="{z:.6g}"/>' for x, y, z in verts)
        tx = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in tris)
        objects.append(
            f'  <object id="{n}" p:UUID="{uuid.uuid4()}" type="model">'
            f"<mesh><vertices>{vx}</vertices><triangles>{tx}</triangles></mesh></object>\n"
        )
    sub = _header() + " <resources>\n" + "".join(objects) + " </resources>\n <build/>\n</model>\n"

    top_id = len(parts) + 1
    comps = "".join(
        f'<component p:path="/3D/Objects/object_1.model" objectid="{n}" p:UUID="{uuid.uuid4()}" '
        'transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
        for n in range(1, len(parts) + 1)
    )
    top = (
        _header(' <metadata name="Application">os2slice</metadata>\n')
        + f' <resources>\n  <object id="{top_id}" p:UUID="{uuid.uuid4()}" type="model">'
        f"<components>{comps}</components></object>\n </resources>\n"
        f' <build p:UUID="{uuid.uuid4()}"><item objectid="{top_id}" p:UUID="{uuid.uuid4()}" '
        'transform="1 0 0 0 1 0 0 0 1 0 0 0" printable="1"/></build>\n</model>\n'
    )

    part_meta = "".join(
        f'    <part id="{n}" subtype="normal_part">\n'
        f'      <metadata key="name" value="{escape(p.name)}"/>\n'
        '      <metadata key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>\n'
        f'      <metadata key="extruder" value="{int(p.filament)}"/>\n'
        "    </part>\n"
        for n, p in enumerate(parts, start=1)
    )
    settings = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n'
        f'  <object id="{top_id}">\n'
        f'    <metadata key="name" value="{escape(object_name)}"/>\n'
        f'    <metadata key="extruder" value="{int(parts[0].filament)}"/>\n'
        f"{part_meta}  </object>\n{_plate(top_id, filament_maps)}</config>\n"
    )

    types = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="model" '
        'ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>\n'
    )

    def rels(target: str) -> str:
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Target="{target}" Id="rel-1" Type="{MODEL_REL}"/></Relationships>\n'
        )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", types)
        z.writestr("_rels/.rels", rels("/3D/3dmodel.model"))
        z.writestr("3D/3dmodel.model", top)
        z.writestr("3D/_rels/3dmodel.model.rels", rels("/3D/Objects/object_1.model"))
        z.writestr("3D/Objects/object_1.model", sub)
        z.writestr("Metadata/model_settings.config", settings)
    return buf.getvalue()


PROJECT_SETTINGS = "Metadata/project_settings.config"
APP_RE = re.compile(r'(<metadata name="Application">)[^<]*(</metadata>)')
VERSION_RE = re.compile(r"\d{2}\.\d{2}\.\d{2}\.\d{2}")


def with_project_settings(threemf: bytes, project_settings: bytes) -> bytes:
    """Add Bambu Studio project settings (printer, filament and process presets) to a 3MF.

    Bambu Studio loads a 3MF's settings only when its Application metadata starts with
    "BambuStudio-" (else: "The 3mf is not from Bambu Lab, load geometry data only"), so
    the file is labelled with the Bambu Studio version that wrote the settings.
    """
    try:
        version = str(json.loads(project_settings).get("version", ""))
    except ValueError as e:
        raise ValueError("project settings aren't JSON") from e
    if not VERSION_RE.fullmatch(version):
        raise ValueError(f"unexpected project settings version {version[:20]!r}")
    src = zipfile.ZipFile(io.BytesIO(threemf))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename == "3D/3dmodel.model":
                data = APP_RE.sub(rf"\g<1>BambuStudio-{version}\g<2>", data.decode(), 1).encode()
            if info.filename != PROJECT_SETTINGS:
                z.writestr(info.filename, data)
        z.writestr(PROJECT_SETTINGS, project_settings)
    return buf.getvalue()


def project_settings_of(threemf: bytes) -> bytes:
    """The project settings of a 3MF written by Bambu Studio (e.g. a BamBuddy slice)."""
    try:
        with zipfile.ZipFile(io.BytesIO(threemf)) as z:
            return z.read(PROJECT_SETTINGS)
    except (zipfile.BadZipFile, KeyError) as e:
        raise ValueError("the sliced file has no project settings") from e
