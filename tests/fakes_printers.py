"""In-memory fakes of Moonraker and PrusaLink, shaped like their documented replies.

Shapes come from Moonraker's external_api docs and Prusa-Link-Web's spec/openapi.yaml
(docs/PRINTER_APIS.md); none is recorded from a live printer yet.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

MOONRAKER_URL = "http://voron.test:7125"
PRUSALINK_URL = "http://mk4.test"


def _wrap(result: Any) -> dict[str, Any]:
    return {"result": result}


@dataclass
class FakeMoonraker:
    klippy_state: str = "ready"
    print_state: str = "standby"
    sd_active: bool = False
    progress: float = 0.0
    message: str = ""
    spool_id: int | None = None
    spool_v2_error: bool = False
    api_key: str | None = None  # when set, requests without it get 401
    fail_with: int | None = None  # every request answers with this status
    print_started: bool = True
    mmu: dict[str, Any] | None = None  # Happy Hare's "mmu" object (HAPPY_HARE), when fitted
    spools: dict[int, dict[str, Any]] = field(default_factory=dict)  # Spoolman, by spool id
    requests: list[httpx.Request] = field(default_factory=list)
    uploads: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_with:
            err = {"error": {"code": self.fail_with, "message": "Internal Server Error"}}
            return httpx.Response(self.fail_with, json=err)
        if self.api_key and request.headers.get("X-Api-Key") != self.api_key:
            return httpx.Response(401, json={"error": {"code": 401, "message": "Unauthorized"}})
        path, m = request.url.path, request.method
        if path == "/server/info":
            return httpx.Response(
                200,
                json=_wrap(
                    {
                        "klippy_connected": self.klippy_state != "disconnected",
                        "klippy_state": self.klippy_state,
                        "components": ["file_manager", "spoolman"],
                        "registered_directories": ["config", "gcodes"],
                        "moonraker_version": "v0.9.3-12-gabcdef0",
                        "api_version": [1, 5, 0],
                        "api_version_string": "1.5.0",
                    }
                ),
            )
        if path == "/printer/info":
            if self.klippy_state != "ready":
                return httpx.Response(
                    503, json={"error": {"code": 503, "message": "Klippy Host not connected"}}
                )
            return httpx.Response(
                200,
                json=_wrap(
                    {
                        "state": "ready",
                        "state_message": "Printer is ready",
                        "hostname": "voron24",
                        "software_version": "v0.12.0-85-gd785b396",
                    }
                ),
            )
        if path == "/printer/objects/query":
            if self.klippy_state != "ready":
                return httpx.Response(
                    503, json={"error": {"code": 503, "message": "Klippy Host not connected"}}
                )
            status = {
                "webhooks": {"state": "ready", "state_message": "Printer is ready"},
                "print_stats": {
                    "filename": "os2slice/a.gcode" if self.print_state != "standby" else "",
                    "state": self.print_state,
                    "message": self.message,
                    "info": {"total_layer": None, "current_layer": None},
                },
                "virtual_sdcard": {"progress": self.progress, "is_active": self.sd_active},
                "extruder": {"temperature": 214.6, "target": 215.0},
                "heater_bed": {"temperature": 59.9, "target": 60.0},
                "toolhead": {"homed_axes": "xyz"},
            }
            if self.mmu is not None:
                status["mmu"] = self.mmu
            return httpx.Response(200, json=_wrap({"eventtime": 1.0, "status": status}))
        if path == "/server/spoolman/spool_id":
            return httpx.Response(200, json=_wrap({"spool_id": self.spool_id}))
        if path == "/server/spoolman/proxy" and m == "POST":
            body = json.loads(request.content)
            wanted = int(body["path"].rsplit("/", 1)[1])
            if wanted in self.spools:
                return httpx.Response(
                    200, json=_wrap({"response": self.spools[wanted], "error": None})
                )
            if self.spool_v2_error:
                err = {"status_code": 404, "message": f"No spool with ID {self.spool_id} found."}
                return httpx.Response(200, json=_wrap({"response": None, "error": err}))
            assert body["path"] == f"/v1/spool/{self.spool_id}"
            spool = {
                "id": self.spool_id,
                "filament": {
                    "id": 2,
                    "name": "Reactor Red",
                    "vendor": {"id": 2, "name": "Fusion"},
                    "material": "PLA",
                    "color_hex": "bd0b0b",
                },
                "remaining_weight": 950,
            }
            return httpx.Response(200, json=_wrap({"response": spool, "error": None}))
        if path == "/server/files/upload" and m == "POST":
            form = _multipart(request)
            self.uploads.append(form)
            start = form.get("print") == "true"
            item = {
                "path": f"{form['path']}/{form['file_name']}",
                "root": form.get("root", "gcodes"),
                "size": len(form["file"]),
                "permissions": "rw",
            }
            return httpx.Response(
                201,
                json=_wrap(
                    {
                        "item": item,
                        "print_started": start and self.print_started,
                        "print_queued": False,
                        "action": "create_file",
                    }
                ),
            )
        return httpx.Response(404, json={"error": {"code": 404, "message": "Not Found"}})


def _multipart(request: httpx.Request) -> dict[str, Any]:
    """Parse a multipart/form-data body into {field: value}, plus `file_name`."""
    ctype = request.headers["Content-Type"]
    assert ctype.startswith("multipart/form-data; boundary=")
    boundary = ctype.split("boundary=", 1)[1].encode()
    out: dict[str, Any] = {}
    for part in request.content.split(b"--" + boundary):
        if part.startswith(b"--"):  # the closing delimiter
            continue
        part = part.removeprefix(b"\r\n").removesuffix(b"\r\n")
        if not part:
            continue
        head, _, body = part.partition(b"\r\n\r\n")
        disp = head.decode()
        name = disp.split('name="', 1)[1].split('"', 1)[0]
        if 'filename="' in disp:
            out["file_name"] = disp.split('filename="', 1)[1].split('"', 1)[0]
            out[name] = body
        else:
            out[name] = body.decode()
    return out


@dataclass
class FakePrusaLink:
    state: str = "IDLE"
    api_key: str = "secret-key"
    existing: set[str] = field(default_factory=set)  # paths under /api/v1/files/
    fail_with: int | None = None  # PUT answers with this status
    printer_ok: bool = True
    requests: list[httpx.Request] = field(default_factory=list)
    uploads: list[httpx.Request] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("X-Api-Key") != self.api_key:
            return httpx.Response(401, text="Unauthorized")
        path, m = request.url.path, request.method
        if path == "/api/version":
            return httpx.Response(
                200,
                json={
                    "api": "2.0.0",
                    "version": "2.1.2",
                    "printer": "1.0.0",
                    "text": "PrusaLink",
                    "firmware": "6.1.3+8215",
                    "capabilities": {"upload-by-put": True},
                },
            )
        if path == "/api/v1/info":
            return httpx.Response(
                200,
                json={
                    "name": "MK4 left",
                    "hostname": "prusa-mk4",
                    "nozzle_diameter": 0.4,
                    "serial": "FAKE0001",
                    "mmu": False,
                },
            )
        if path == "/api/v1/status":
            body: dict[str, Any] = {
                "printer": {
                    "state": self.state,
                    "temp_nozzle": 214.9,
                    "target_nozzle": 215.0,
                    "temp_bed": 59.5,
                    "target_bed": 60.0,
                    "status_printer": {
                        "ok": self.printer_ok,
                        "message": "OK" if self.printer_ok else "Printer not connected",
                    },
                },
                "storage": {"name": "usb", "path": "/usb", "read_only": False},
            }
            if self.state in ("PRINTING", "PAUSED"):
                body["job"] = {"id": 420, "progress": 42.0, "time_remaining": 3720}
            return httpx.Response(200, json=body)
        if path.startswith("/api/v1/files/") and m == "PUT":
            self.uploads.append(request)
            if self.fail_with:
                err = {"title": "Storage full", "text": "Not enough space on the USB drive."}
                return httpx.Response(self.fail_with, json=err)
            where = path.removeprefix("/api/v1/files/")
            if where in self.existing:
                return httpx.Response(409, text="File already exists.")
            self.existing.add(where)
            return httpx.Response(201)
        return httpx.Response(404, json={"title": "Not Found", "text": "No such endpoint"})


# Happy Hare on a 5-gate MMU, as joshprint's Moonraker reported it (2026-10-02).
HAPPY_HARE: dict[str, Any] = {
    "enabled": True,
    "num_gates": 5,
    "ttg_map": [0, 1, 2, 3, 4],
    "gate_status": [1, 1, 1, 1, 0],
    "gate_material": ["ASA", "PETG", "PETG", "ASA", "ASA"],
    "gate_color": ["000000", "000000", "00ffffff", "ffffffff", "ff8400"],
    "gate_filament_name": ["Black", "PolyLite™ ASA Blue", "CR-PETG Transparent", "ASA White",
                           "Orange ASA"],
    "gate_spool_id": [7, -1, 8, 10, 9],
}  # fmt: skip
HAPPY_HARE_SPOOLS = {
    7: {"id": 7, "filament": {"name": "Black", "vendor": {"name": "Ambrosia"}, "material": "ASA"}},
    8: {"id": 8, "filament": {"name": "CR-PETG Transparent", "vendor": {"name": "Creality"},
                              "material": "PETG"}},
    9: {"id": 9, "filament": {"name": "Orange ASA", "vendor": {"name": "3DO"}, "material": "ASA"}},
    10: {"id": 10, "filament": {"name": "ASA White", "vendor": {"name": "Elegoo"},
                                "material": "ASA"}},
}  # fmt: skip
