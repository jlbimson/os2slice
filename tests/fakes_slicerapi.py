"""In-memory fake of the OrcaSlicer / Bambu Studio API sidecars (docs/SLICERAPI_API.md).

Shapes are the ones recorded from `ghcr.io/afkfelix/orca-slicer-api:latest-orca2.4.2`
("afk") and `ghcr.io/maziggy/bambu-studio-api:latest` ("resolver") on 2026-10-01.
"""

from __future__ import annotations

import io
import json
import struct
import threading
import zipfile
from dataclasses import dataclass, field
from email.parser import BytesParser
from email.policy import default as email_policy
from typing import Any

import httpx

AFK_HEALTH = {
    "status": "healthy",
    "timestamp": "2026-10-01T21:52:23.043Z",
    "checks": {
        "orcaslicer": {"available": True, "version": "2.4.2"},
        "systemProfilePath": {"accessible": True},
    },
}
RESOLVER_HEALTH = {
    "status": "unhealthy",
    "timestamp": "2026-10-01T21:52:23.111Z",
    "checks": {
        "orcaslicer": {"available": True, "version": "unknown"},
        "dataPath": {
            "accessible": False,
            "error": "ENOENT: no such file or directory, access '/app/data'",
        },
    },
}
A1M = "Bambu Lab A1 mini 0.4 nozzle"
X1C = "Bambu Lab X1 Carbon 0.4 nozzle"
BUNDLED = {
    "printer": [
        {"name": A1M, "base_id": "fdm_bbl_3dp_001_common"},
        {"name": X1C, "base_id": "fdm_bbl_3dp_001_common"},
    ],
    "process": [
        {"name": "0.20mm Standard @BBL A1M", "base_id": "x", "compatible_printers": [A1M]},
        {"name": "0.20mm Standard @BBL X1C", "base_id": "x", "compatible_printers": [X1C]},
        {"name": "0.20mm Odd", "base_id": "x", "compatible_printers": None},
    ],
    "filament": [
        {
            "name": "Bambu PLA Basic @BBL A1M",
            "base_id": "x",
            "compatible_printers": [A1M],
            "filament_type": "PLA",
            "filament_colour": None,
        },
        {
            "name": "Generic PETG @BBL X1C",
            "base_id": "x",
            "compatible_printers": [X1C],
            "filament_type": "PETG",
            "filament_colour": None,
        },
    ],
}
AFK_NAMES = {
    "printers": [A1M, X1C, "Anker M5 0.4 nozzle"],
    "presets": ["0.20mm Standard @BBL A1M", "0.20mm Standard @BBL X1C", "0.20mm Standard"],
    "filaments": ["Bambu PLA Basic @BBL A1M", "Generic PLA", "Generic PETG @BBL X1C"],
}
REQUEST_ID = "272439be-522a-4561-8495-fb523306030d"


def gcode(layers: int = 32, grams: str = "5.88", minutes: int = 15) -> bytes:
    """A Bambu Studio style G-code header."""
    return (
        "; HEADER_BLOCK_START\n; BambuStudio 02.08.02.61\n"
        f"; model printing time: 9m 7s; total estimated time: {minutes}m 5s\n"
        f"; total layer number: {layers}\n"
        f"; total filament weight [g] : {grams}\n; HEADER_BLOCK_END\nG28\n"
    ).encode()


def gcode_3mf(prediction: int | None = 905, weight: str | None = "5.88", layers: int = 32) -> bytes:
    info = '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n  <plate>\n'
    if prediction is not None:
        info += f'    <metadata key="prediction" value="{prediction}"/>\n'
    if weight is not None:
        info += f'    <metadata key="weight" value="{weight}"/>\n'
    info += "  </plate>\n</config>\n"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Metadata/plate_1.gcode", gcode(layers, "5.64,5.31"))
        z.writestr("Metadata/slice_info.config", info)
        z.writestr("Metadata/project_settings.config", "{}")
    return buf.getvalue()


def box_stl(w: float, d: float, h: float, x: float = 0.0, y: float = 0.0) -> bytes:
    """A closed box (12 facets) with its min corner at (x, y, 0)."""
    p = [(x + i * w, y + j * d, k * h) for i in (0, 1) for j in (0, 1) for k in (0, 1)]
    faces = [
        (0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5), (0, 4, 5), (0, 5, 1),
        (2, 3, 7), (2, 7, 6), (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3),
    ]  # fmt: skip
    body = b"".join(struct.pack("<12fH", 0, 0, 0, *p[a], *p[b], *p[c], 0) for a, b, c in faces)
    return b"\0" * 80 + struct.pack("<I", len(faces)) + body


@dataclass
class Part:
    name: str
    filename: str | None
    content_type: str
    data: bytes

    def json(self) -> Any:
        return json.loads(self.data)


def parse_multipart(request: httpx.Request) -> list[Part]:
    head = f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode()
    msg = BytesParser(policy=email_policy).parsebytes(head + request.content)
    parts = []
    for p in msg.iter_parts():
        parts.append(
            Part(
                p.get_param("name", header="content-disposition") or "",
                p.get_filename(),
                p.get_content_type(),
                p.get_payload(decode=True) or b"",
            )
        )
    return parts


@dataclass
class FakeSidecar:
    """`flavour` "afk" or "resolver"; tweak the attributes to script a scenario."""

    flavour: str = "resolver"
    statuses: list[str] = field(default_factory=lambda: ["pending", "processing", "completed"])
    fail_message: str = "Slicing failed with error from slicer: boom"
    result: bytes | None = None  # default: a .gcode.3mf, or G-code without exportType=3mf
    result_headers: dict[str, str] = field(
        default_factory=lambda: {
            "X-Print-Time-Seconds": "905",
            "X-Filament-Used-g": "5.88",
            "X-Filament-Used-mm": "1939.73",
        }
    )
    slice_error: tuple[int, dict[str, Any]] | None = None
    progress: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {"stage": "Generating G-code", "total_percent": 75, "plate_percent": 80}
        ]
    )
    block: threading.Event | None = None  # sync POST /slice waits on it (timeout tests)
    health: tuple[int, Any] | None = None
    unknown_profiles: set[str] = field(default_factory=set)
    requests: list[httpx.Request] = field(default_factory=list)
    uploads: list[list[Part]] = field(default_factory=list)
    _polls: int = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    @property
    def paths(self) -> list[str]:
        return [f"{r.method} {r.url.path}" for r in self.requests]

    def fields(self, n: int = -1) -> dict[str, str]:
        return {p.name: p.data.decode() for p in self.uploads[n] if p.filename is None}

    def files(self, n: int = -1, name: str | None = None) -> list[Part]:
        return [p for p in self.uploads[n] if p.filename and (name is None or p.name == name)]

    def _result(self) -> bytes:
        if self.result is not None:
            return self.result
        return gcode_3mf() if self.fields().get("exportType") == "3mf" else gcode()

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, method = request.url.path, request.method
        if path == "/health" and method == "GET":
            if self.health is not None:
                return httpx.Response(self.health[0], json=self.health[1])
            body = AFK_HEALTH if self.flavour == "afk" else RESOLVER_HEALTH
            return httpx.Response(200 if body["status"] == "healthy" else 503, json=body)
        if path == "/profiles/bundled" and method == "GET":
            if self.flavour == "afk":
                return httpx.Response(400, json={"message": "Invalid or missing category"})
            return httpx.Response(200, json=BUNDLED)
        if path.startswith("/profiles/") and method == "GET":
            rest = path.removeprefix("/profiles/").split("/", 1)
            if self.flavour == "resolver":
                return httpx.Response(200, json=[])  # stored user profiles: none
            if len(rest) == 1:
                return httpx.Response(200, json=AFK_NAMES.get(rest[0], []))
            name = rest[1]
            if name in self.unknown_profiles:
                return httpx.Response(
                    404, json={"message": f'Profile "{name}" not found in category "{rest[0]}".'}
                )
            return httpx.Response(200, json={"name": name, "type": "process", "from": "system"})
        if path == "/slice" and method == "POST":
            self.uploads.append(parse_multipart(request))
            if self.block is not None:
                self.block.wait(10)
            if self.slice_error:
                return httpx.Response(self.slice_error[0], json=self.slice_error[1])
            return httpx.Response(200, content=self._result(), headers=self.result_headers)
        if path.startswith("/slice/progress/") and method == "GET":
            if self.flavour == "afk":
                return httpx.Response(404, text="<pre>Cannot GET</pre>")
            if not self.progress:
                return httpx.Response(404, json={"error": "not_found"})
            return httpx.Response(200, json=self.progress.pop(0))
        if path == "/slice-async" and method == "POST":
            self.uploads.append(parse_multipart(request))
            if self.slice_error:
                return httpx.Response(self.slice_error[0], json=self.slice_error[1])
            return httpx.Response(
                202,
                json={
                    "requestId": REQUEST_ID,
                    "status": "pending",
                    "statusUrl": f"/slice-async/{REQUEST_ID}",
                },
            )
        if path == f"/slice-async/{REQUEST_ID}" and method == "GET":
            status = self.statuses[min(self._polls, len(self.statuses) - 1)]
            self._polls += 1
            body: dict[str, Any] = {"requestId": REQUEST_ID, "status": status}
            if status == "failed":
                body["message"] = self.fail_message
            if status == "completed":
                body["metadata"] = {"printTime": 972, "filamentUsedG": 5.9, "filamentUsedMm": 1950}
                body["downloadUrl"] = f"/slice-async/{REQUEST_ID}/result"
            return httpx.Response(200, json=body)
        if path == f"/slice-async/{REQUEST_ID}/result" and method == "GET":
            return httpx.Response(200, content=self._result(), headers=self.result_headers)
        if path == f"/slice-async/{REQUEST_ID}" and method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(404, json={"message": "Slice request not found"})
