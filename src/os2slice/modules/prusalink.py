"""PrusaLink target (MK4/S, XL, Core One, MINI, MK3.5/3.9): upload, status.

Endpoint shapes are read from Prusa-Link-Web's spec/openapi.yaml (docs/PRINTER_APIS.md);
none is verified against a live printer yet. Only the configured `url` is called.
Auth is the printer's API key in `X-Api-Key`; HTTP digest (user "maker") is not
supported yet.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any, ClassVar
from urllib.parse import quote, urlsplit

import httpx

from os2slice import __version__
from os2slice.errors import ConfigError
from os2slice.modules.base import (
    MEDIA_BGCODE,
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

STORAGES = ("usb", "local")

SPEC = ModuleSpec(
    kind="prusalink",
    label="PrusaLink (Prusa MK4, XL, Core One, MINI)",
    role="target",
    technology="fdm",
    accepts=(MEDIA_GCODE, MEDIA_BGCODE),
    fields=(
        Field("url", "Printer URL", "url", required=True, help="e.g. http://192.168.1.50"),
        Field(
            "api_key",
            "API key",
            "secret",
            required=True,
            help="Shown on the printer: Settings > Network > PrusaLink. Sent as X-Api-Key.",
        ),
        Field(
            "storage",
            "Storage",
            "choice",
            default="usb",
            choices=STORAGES,
            help="usb on xBuddy printers (MK4, XL, MINI, Core One); local on a PrusaLink Pi.",
        ),
        Field("folder", "Upload folder", "str", default="os2slice"),
    ),
    help="Prusa printers through PrusaLink. Saves the file on the printer; a person starts it.",
)

READY_STATES = frozenset({"IDLE", "FINISHED", "STOPPED", "READY"})
BUSY_STATES = frozenset({"PRINTING", "PAUSED"})
EXTENSIONS = {MEDIA_GCODE: ".gcode", MEDIA_BGCODE: ".bgcode"}
SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
TIMEOUT = httpx.Timeout(10.0)
UPLOAD_TIMEOUT = httpx.Timeout(10.0, read=120.0, write=600.0)  # USB writes are slow


class PrusaLinkError(ModuleError):
    """A ModuleError that remembers the HTTP status (None: the printer didn't answer)."""

    def __init__(self, message: str, fix: str = "", status: int | None = None) -> None:
        super().__init__(message, fix)
        self.status = status


class PrusaLinkAuthError(PrusaLinkError, ModuleAuthError):
    """PrusaLink refused the API key (401/403)."""


def check_url(url: str, what: str) -> str:
    """A configured http(s) base URL with no credentials, query or fragment."""
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


def file_name(filename: str, ext: str, stamp: str = "") -> str:
    """`output.filename` made safe; with `stamp`, `<stem>_<stamp><ext>`."""
    stem = filename[: -len(ext)] if filename.lower().endswith(ext) else filename
    stem = safe_segment(stem, "part")
    return f"{stem}_{stamp}{ext}" if stamp else f"{stem}{ext}"


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _duration(seconds: float) -> str:
    m = int(seconds) // 60
    return f"{m // 60} h {m % 60:02d} min" if m >= 60 else f"{m} min"


class PrusaLink:
    spec: ClassVar[ModuleSpec] = SPEC

    def __init__(
        self,
        values: Mapping[str, Any],
        *,
        key: str = "prusalink",
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.key = key
        self.url = check_url(str(values.get("url") or ""), "PrusaLink url")
        key = str(values.get("api_key") or "")
        if not key:
            raise ConfigError(
                "PrusaLink needs the printer's API key",
                "Find it on the printer under Settings > Network > PrusaLink",
            )
        storage = str(values.get("storage") or "usb")
        if storage not in STORAGES:
            raise ConfigError(f"PrusaLink storage must be usb or local, not {storage!r}")
        self.storage = storage
        self.folder = safe_segment(str(values.get("folder", "os2slice") or ""))
        self._clock = clock
        self._client = httpx.Client(
            base_url=self.url,
            headers={
                "User-Agent": f"os2slice/{__version__}",
                "Accept": "application/json",
                "X-Api-Key": key,
            },
            timeout=TIMEOUT,
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PrusaLink:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- Target --------------------------------------------------------------

    def check(self) -> Health:
        try:
            ver = self._get("/api/version")
            info = self._get("/api/v1/info")
        except ModuleError as e:
            return Health(False, e.one_line())
        firmware = str(ver.get("firmware") or ver.get("server") or ver.get("version") or "")
        text = str(ver.get("text") or "PrusaLink")
        name = str(info.get("name") or info.get("hostname") or "")
        summary = f"{text} {firmware}".strip() + (f" on {name}" if name else "")
        caps = ver.get("capabilities")
        if isinstance(caps, dict) and caps.get("upload-by-put") is False:
            return Health(False, f"{summary}: no upload-by-put", firmware)
        return Health(True, summary, firmware)

    def printers(self, configured: tuple[PrinterInfo, ...]) -> tuple[PrinterInfo, ...]:
        info: dict[str, Any] = {}
        if any(not p.model or "nozzle_diameter" not in p.extra for p in configured):
            try:
                info = self._get("/api/v1/info")
            except ModuleError as e:
                log.warning("prusalink %s: no printer info: %s", self.url, e.one_line())
        model = str(info.get("model") or info.get("hostname") or "")
        nozzle = _num(info.get("nozzle_diameter"))
        out = []
        for p in configured:
            changes: dict[str, Any] = {}
            if not p.model and model:
                changes["model"] = model
            if nozzle is not None and "nozzle_diameter" not in p.extra:
                changes["extra"] = {**p.extra, "nozzle_diameter": nozzle}
            if not p.ui_url:
                changes["ui_url"] = self.url
            out.append(replace(p, **changes) if changes else p)
        return tuple(out)

    def status(self, printer: PrinterInfo) -> PrinterStatus:
        try:
            body = self._get("/api/v1/status")
        except PrusaLinkError as e:
            if e.status is None:
                return PrinterStatus("offline", False, False, e.message)
            raise
        p = body.get("printer") or {}
        job = body.get("job") or {}
        state = str(p.get("state") or "unknown").upper()
        link = p.get("status_printer") or {}
        connected = state != "ERROR" and link.get("ok") is not False
        ready = connected and state in READY_STATES
        bits: list[str] = []
        if state in BUSY_STATES:
            prog = _num(job.get("progress"))
            if prog is not None:
                bits.append(f"{prog:.0f}%")
            left = _num(job.get("time_remaining"))
            if left is not None:
                bits.append(f"{_duration(left)} left")
            for label, t, target in (
                ("nozzle", "temp_nozzle", "target_nozzle"),
                ("bed", "temp_bed", "target_bed"),
            ):
                if _num(p.get(t)) is not None:
                    bits.append(f"{label} {p[t]:.0f}/{_num(p.get(target)) or 0:.0f} °C")
        elif state == "ATTENTION":
            bits.append("needs attention at the printer")
        if link.get("ok") is False and link.get("message"):
            bits.append(str(link["message"]))
        return PrinterStatus(state, connected, ready, ", ".join(bits), raw=body)

    def submit(
        self,
        printer: PrinterInfo,
        output: SliceOutput,
        *,
        start: bool,
        materials: tuple[Material, ...] = (),
        progress: Progress = lambda s: None,
    ) -> Submission:
        ext = EXTENSIONS.get(output.media)
        if ext is None:
            raise ModuleError(
                f"PrusaLink takes .gcode or .bgcode, not {output.media}",
                "Pair this printer with a slicer that makes G-code",
            )
        name = file_name(output.filename, ext)
        progress(f"Uploading {name} to {printer.name}")
        try:
            where = self._put(name, output.data, start)
        except PrusaLinkError as e:
            if e.status != 409:
                raise
            # A file of that name exists (Overwrite: ?0): add a timestamp and try once more.
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self._clock()))
            name = file_name(output.filename, ext, stamp)
            where = self._put(name, output.data, start)
        if start:
            return Submission(where, "started", f"Printing {name}", self.url)
        detail = (
            f"Saved on the printer's {self.storage.upper()} as {name}; "
            "start it from the printer's screen or PrusaLink"
        )
        return Submission(where, "waiting", detail, self.url)

    # -- plumbing ------------------------------------------------------------

    def _put(self, name: str, data: bytes, start: bool) -> str:
        where = "/".join(s for s in (self.storage, self.folder, name) if s)
        headers = {
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(data)),
            "Overwrite": "?0",
            "Print-After-Upload": "?1" if start else "?0",
        }
        log.warning("prusalink %s: uploading %s (start=%s)", self.url, where, start)
        self._request(
            "PUT",
            "/api/v1/files/" + quote(where),
            content=data,
            headers=headers,
            timeout=UPLOAD_TIMEOUT,
        )
        return where

    def _get(self, path: str) -> dict[str, Any]:
        r = self._request("GET", path)
        try:
            body = r.json()
        except ValueError as e:
            raise PrusaLinkError("PrusaLink returned invalid JSON", "", r.status_code) from e
        if not isinstance(body, dict):
            raise PrusaLinkError("PrusaLink returned an unexpected reply", "", r.status_code)
        return body

    def _request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        try:
            r = self._client.request(method, path, **kw)
        except httpx.TimeoutException as e:
            raise PrusaLinkError(f"The printer at {self.url} didn't answer in time") from e
        except httpx.TransportError as e:
            raise PrusaLinkError(
                f"Can't reach PrusaLink at {self.url} ({type(e).__name__})",
                "Check the target's url and that the printer is on",
            ) from e
        log.debug("prusalink %s %s -> %s", method, r.request.url.path, r.status_code)
        if r.is_success:
            return r
        code, reason = r.status_code, self._reason(r)
        key_fix = (
            f"Check the API key for [targets.{self.key}] (secret targets.{self.key}.api_key) "
            "against the printer's Settings > Network > PrusaLink"
        )
        fixes = {
            401: key_fix,
            403: key_fix,
            404: f"Check that the printer's {self.storage} storage is present (USB drive in?)",
            409: "",
            413: "The file is too large for the printer",
            507: f"The printer's {self.storage} storage is full; free some space",
        }
        if code in (401, 403):
            raise PrusaLinkAuthError(f"PrusaLink refused the API key{reason}", key_fix, code)
        raise PrusaLinkError(f"PrusaLink error {code} on {path}{reason}", fixes.get(code, ""), code)

    @staticmethod
    def _reason(r: httpx.Response) -> str:
        try:
            body = r.json()
        except ValueError:
            text = r.text.strip()[:300]
            return f": {text}" if text else ""
        if isinstance(body, dict):
            text = ": ".join(str(body[k]) for k in ("title", "text") if body.get(k))
            return f": {text[:300]}" if text else ""
        return ""
