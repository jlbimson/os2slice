"""Moonraker (Klipper) target: upload G-code, optionally start it, read status.

Endpoint shapes are read from Moonraker's documentation (docs/PRINTER_APIS.md); none
is verified against a live printer yet. Only the configured `url` is ever called.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any, ClassVar
from urllib.parse import urlsplit

import httpx

from os2slice import __version__
from os2slice.errors import ConfigError
from os2slice.modules.base import (
    MEDIA_GCODE,
    Field,
    Health,
    Material,
    ModuleAuthError,
    ModuleError,
    ModuleSpec,
    PrinterInfo,
    PrinterStatus,
    Progress,
    SliceOutput,
    Submission,
)

log = logging.getLogger(__name__)

SPEC = ModuleSpec(
    kind="moonraker",
    label="Moonraker (Klipper)",
    role="target",
    technology="fdm",
    accepts=(MEDIA_GCODE,),
    fields=(
        Field("url", "Moonraker URL", "url", required=True, help="e.g. http://voron.lan:7125"),
        Field(
            "api_key",
            "API key",
            "secret",
            help="Sent as X-Api-Key. Optional: Moonraker trusts LAN ranges by default.",
        ),
        Field("ui_url", "Mainsail / Fluidd URL", "url", help="Where people watch the print."),
        Field("folder", "Upload folder", "str", default="os2slice", help="Subfolder of gcodes."),
        Field(
            "spoolman",
            "Read the active Spoolman spool",
            "bool",
            default=False,
            help="Show Spoolman's active spool as the loaded material.",
        ),
    ),
    help="Klipper printers through Moonraker. Uploads G-code; a person starts the print.",
)

# print_stats.state values that leave the printer free for a new job.
IDLE_STATES = frozenset({"standby", "complete", "cancelled"})
BUSY_STATES = frozenset({"printing", "paused"})
STATUS_QUERY = (
    "/printer/objects/query?webhooks&print_stats&virtual_sdcard&extruder&heater_bed&toolhead"
    "&mmu"  # Happy Hare's filament changer, when there is one (absent objects are left out)
)
MAX_TOOLS = 16  # as many filaments as a slice takes
SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
TIMEOUT = httpx.Timeout(10.0)
UPLOAD_TIMEOUT = httpx.Timeout(10.0, read=120.0, write=300.0)


class MoonrakerError(ModuleError):
    """A ModuleError that remembers the HTTP status (None: Moonraker didn't answer)."""

    def __init__(self, message: str, fix: str = "", status: int | None = None) -> None:
        super().__init__(message, fix)
        self.status = status


class MoonrakerAuthError(MoonrakerError, ModuleAuthError):
    """Moonraker refused the request (401/403): the API key or trusted_clients."""


def check_url(url: str, what: str) -> str:
    """A configured http(s) base URL with no credentials, path or query, without the slash."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError(f"{what} must be an http:// or https:// URL", f"Got {url!r}")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ConfigError(f"{what} must not carry credentials, a query or a fragment")
    return url.strip().rstrip("/")


def safe_segment(text: str, fallback: str = "") -> str:
    """Strictly `[A-Za-z0-9._-]`, no leading dots, at most 80 characters."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = SAFE_RE.sub("_", text).strip("._-")[:80].strip("._-")
    return text or fallback


def unique_name(filename: str, ext: str, now: float) -> str:
    """`output.filename` made safe and unique: `<stem>_<YYYYmmdd-HHMMSS><ext>`."""
    stem = filename[: -len(ext)] if filename.lower().endswith(ext) else filename
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    return f"{safe_segment(stem, 'part')}_{stamp}{ext}"


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _temps(status: Mapping[str, Any]) -> str:
    out = []
    for key, label in (("extruder", "nozzle"), ("heater_bed", "bed")):
        obj = status.get(key) or {}
        t, target = _num(obj.get("temperature")), _num(obj.get("target"))
        if t is not None:
            out.append(f"{label} {t:.0f}/{target or 0:.0f} °C")
    return ", ".join(out)


class Moonraker:
    spec: ClassVar[ModuleSpec] = SPEC

    def __init__(
        self,
        values: Mapping[str, Any],
        *,
        key: str = "moonraker",
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.key = key
        self.url = check_url(str(values.get("url") or ""), "Moonraker url")
        ui = str(values.get("ui_url") or "")
        self.ui_url = check_url(ui, "Moonraker ui_url") if ui else ""
        self.folder = safe_segment(str(values.get("folder", "os2slice") or ""))
        self.spoolman = bool(values.get("spoolman", False))
        self._clock = clock
        headers = {"User-Agent": f"os2slice/{__version__}", "Accept": "application/json"}
        if values.get("api_key"):
            headers["X-Api-Key"] = str(values["api_key"])
        self._client = httpx.Client(
            base_url=self.url,
            headers=headers,
            timeout=TIMEOUT,
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Moonraker:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- Target --------------------------------------------------------------

    def check(self) -> Health:
        try:
            server = self._get("/server/info")
            state = str(server.get("klippy_state") or "unknown")
            version = str(server.get("moonraker_version") or "")
            if state != "ready" or not server.get("klippy_connected", True):
                return Health(False, f"Moonraker {version}: Klipper is {state}", version)
            info = self._get("/printer/info")
        except ModuleError as e:
            return Health(False, e.one_line())
        klipper = str(info.get("software_version") or "")
        summary = f"Moonraker {version}, Klipper {klipper}: {state}"
        return Health(True, summary, version)

    def printers(self, configured: tuple[PrinterInfo, ...]) -> tuple[PrinterInfo, ...]:
        hostname = ""
        if any(not p.model for p in configured):
            try:
                hostname = str(self._get("/printer/info").get("hostname") or "")
            except ModuleError as e:
                log.warning("moonraker %s: no printer info: %s", self.url, e.one_line())
        out = []
        for p in configured:
            changes: dict[str, Any] = {}
            if not p.model and hostname:
                changes["model"] = hostname
            if not p.ui_url and self.ui_url:
                changes["ui_url"] = self.ui_url
            out.append(replace(p, **changes) if changes else p)
        return tuple(out)

    def status(self, printer: PrinterInfo) -> PrinterStatus:
        try:
            body = self._get(STATUS_QUERY)
        except MoonrakerError as e:
            if e.status is None:
                return PrinterStatus("offline", False, False, e.message)
            if e.status < 500:
                raise
            # Moonraker answers object queries with an error while Klippy is down.
            server = self._get("/server/info")
            klippy = str(server.get("klippy_state") or "unknown")
            if klippy == "ready":
                raise
            return PrinterStatus(f"klipper {klippy}", False, False, e.message, raw=server)
        status = body.get("status") or {}
        klippy = str((status.get("webhooks") or {}).get("state") or "unknown")
        stats = status.get("print_stats") or {}
        sdcard = status.get("virtual_sdcard") or {}
        if klippy != "ready":
            msg = str((status.get("webhooks") or {}).get("state_message") or "")
            return PrinterStatus(f"klipper {klippy}", False, False, msg, raw=status)
        state = str(stats.get("state") or "unknown")
        active = bool(sdcard.get("is_active"))
        ready = (state in IDLE_STATES or state == "error") and not active
        detail = ""
        if state in BUSY_STATES:
            progress = _num(sdcard.get("progress")) or 0.0
            bits = [f"{progress * 100:.0f}%", _temps(status)]
            detail = ", ".join(b for b in bits if b)
        elif state == "error":
            detail = str(stats.get("message") or "")
        materials = self._tools(status.get("mmu")) or self._materials()
        return PrinterStatus(state, True, ready, detail, materials, raw=status)

    def submit(
        self,
        printer: PrinterInfo,
        output: SliceOutput,
        *,
        start: bool,
        materials: tuple[Material, ...] = (),
        progress: Progress = lambda s: None,
    ) -> Submission:
        if output.media != MEDIA_GCODE:
            raise ModuleError(
                f"Moonraker takes plain G-code, not {output.media}",
                "Pair this printer with a slicer that makes .gcode",
            )
        name = unique_name(output.filename, ".gcode", self._clock())
        form = {"root": "gcodes", "path": self.folder}
        if start:
            form["print"] = "true"
        progress(f"Uploading {name} to {printer.name}")
        log.warning("moonraker %s: uploading %s (start=%s)", self.url, name, start)
        body = self._json(
            self._request(
                "POST",
                "/server/files/upload",
                data=form,
                files={"file": (name, output.data, "application/octet-stream")},
                timeout=UPLOAD_TIMEOUT,
            )
        )
        item = body.get("item") or {}
        where = str(item.get("path") or (f"{self.folder}/{name}" if self.folder else name))
        if not start:
            return Submission(
                where,
                "waiting",
                f"Uploaded to {where}; start it from Mainsail or Fluidd",
                self.ui_url,
                raw=body,
            )
        if body.get("print_started"):
            return Submission(where, "started", f"Printing {where}", self.ui_url, raw=body)
        if body.get("print_queued"):
            detail = f"Queued {where} in Moonraker's job queue"
            return Submission(where, "started", detail, self.ui_url, raw=body)
        detail = f"Uploaded to {where}, but Moonraker didn't start it; start it from Mainsail"
        return Submission(where, "waiting", detail, self.ui_url, raw=body)

    # -- Happy Hare (MMU) -----------------------------------------------------

    def _tools(self, mmu: Any) -> tuple[Material, ...]:
        """The filament changer's tools, T0 first: each the gate its tool map points at,
        with the gate's material, colour and name, and its Spoolman spool's vendor. The
        core matches them to filament profiles (Material.raw["tool"] marks a tool)."""
        if not isinstance(mmu, dict) or not mmu.get("enabled"):
            return ()
        ttg = mmu.get("ttg_map")
        if not isinstance(ttg, list):
            return ()

        def at(key: str, gate: int) -> Any:
            values = mmu.get(key)
            return values[gate] if isinstance(values, list) and gate < len(values) else None

        tools = []
        for tool, gate in enumerate(ttg[:MAX_TOOLS]):
            if not isinstance(gate, int) or isinstance(gate, bool) or gate < 0:
                continue
            material = str(at("gate_material", gate) or "").strip()[:40]
            name = str(at("gate_filament_name", gate) or "").strip()[:80]
            hexes = str(at("gate_color", gate) or "")[:6]
            colour = f"#{hexes.upper()}" if re.fullmatch(r"[0-9A-Fa-f]{6}", hexes) else None
            spool = at("gate_spool_id", gate)
            vendor = self._spool_vendor(spool) if isinstance(spool, int) and spool >= 0 else ""
            empty = at("gate_status", gate) == 0
            shown = name or material or "unknown filament"
            if material and material.upper() not in shown.upper():
                shown += f" ({material})"
            label = f"T{tool}: {'empty' if empty else shown}"
            raw = {"tool": tool, "gate": gate, "name": name, "vendor": vendor, "empty": empty}
            tools.append(Material(f"t{tool}", label, material, colour, raw=raw))
        return tuple(tools)

    def _spool_vendor(self, spool_id: int) -> str:
        try:
            body = self._json(
                self._request(
                    "POST",
                    "/server/spoolman/proxy",
                    json={"use_v2_response": True, "request_method": "GET",
                          "path": f"/v1/spool/{spool_id}"},
                )
            )  # fmt: skip
        except ModuleError as e:
            log.warning("moonraker %s: no Spoolman spool %s: %s", self.url, spool_id, e.one_line())
            return ""
        spool = body.get("response") if isinstance(body.get("response"), dict) else body
        vendor = ((spool or {}).get("filament") or {}).get("vendor") or {}
        return str(vendor.get("name") or "")[:40] if isinstance(vendor, dict) else ""

    # -- Spoolman ------------------------------------------------------------

    def _materials(self) -> tuple[Material, ...]:
        if not self.spoolman:
            return ()
        try:
            spool_id = self._get("/server/spoolman/spool_id").get("spool_id")
            if not isinstance(spool_id, int) or isinstance(spool_id, bool):
                return ()
            proxied = self._json(
                self._request(
                    "POST",
                    "/server/spoolman/proxy",
                    json={
                        "use_v2_response": True,
                        "request_method": "GET",
                        "path": f"/v1/spool/{spool_id}",
                    },
                )
            )
        except ModuleError as e:
            log.warning("moonraker %s: no Spoolman spool: %s", self.url, e.one_line())
            return ()
        spool = proxied
        if "response" in proxied or "error" in proxied:  # version 2 response
            if proxied.get("error"):
                log.warning("moonraker %s: Spoolman said %s", self.url, proxied["error"])
                return ()
            spool = proxied.get("response") or {}
        if not isinstance(spool, dict):
            return ()
        fil = spool.get("filament") or {}
        vendor = str((fil.get("vendor") or {}).get("name") or "")
        material = str(fil.get("material") or "")
        name = " ".join(s for s in (vendor, str(fil.get("name") or "")) if s)
        hexes = str(fil.get("color_hex") or "")
        colour = f"#{hexes.upper()}" if re.fullmatch(r"[0-9A-Fa-f]{6}", hexes) else None
        label = f"Spool {spool_id}: " + ", ".join(s for s in (material, name) if s)
        return (Material(str(spool_id), label, material, colour, raw=spool),)

    # -- plumbing ------------------------------------------------------------

    def _get(self, path: str) -> dict[str, Any]:
        return self._json(self._request("GET", path))

    def _request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        try:
            r = self._client.request(method, path, **kw)
        except httpx.TimeoutException as e:
            raise MoonrakerError(f"Moonraker at {self.url} didn't answer in time") from e
        except httpx.TransportError as e:
            raise MoonrakerError(
                f"Can't reach Moonraker at {self.url} ({type(e).__name__})",
                "Check the target's url and that the printer is on",
            ) from e
        log.debug("moonraker %s %s -> %s", method, r.request.url.path, r.status_code)
        if r.is_success:
            return r
        reason = self._reason(r)
        if r.status_code in (401, 403):
            raise MoonrakerAuthError(
                f"Moonraker refused the request ({r.status_code}{reason})",
                f"Check the API key for [targets.{self.key}] (secret targets.{self.key}.api_key), "
                "or add this server to Moonraker's trusted_clients",
                r.status_code,
            )
        raise MoonrakerError(
            f"Moonraker error {r.status_code} on {r.request.url.path}{reason}", "", r.status_code
        )

    @staticmethod
    def _reason(r: httpx.Response) -> str:
        try:
            body = r.json()
        except ValueError:
            text = r.text.strip()[:300]
            return f": {text}" if text else ""
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict) and err.get("message"):
            return f": {str(err['message'])[:300]}"
        return ""

    @staticmethod
    def _json(r: httpx.Response) -> dict[str, Any]:
        """The response's `result` object (Moonraker wraps nearly every reply in one)."""
        try:
            body = r.json()
        except ValueError as e:
            raise ModuleError("Moonraker returned invalid JSON") from e
        if isinstance(body, dict) and "result" in body:
            body = body["result"]
        if not isinstance(body, dict):
            raise ModuleError("Moonraker returned an unexpected reply")
        return body
