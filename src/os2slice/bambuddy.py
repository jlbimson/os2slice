"""Thin BamBuddy REST client. Request shapes are verified in docs/BAMBUDDY_API.md."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from os2slice import __version__
from os2slice.errors import AuthError
from os2slice.modules.base import ModuleError
from os2slice.settings import PrintSettings

log = logging.getLogger(__name__)

FAILED_STATES = frozenset({"failed", "error", "cancelled", "canceled"})
DONE_STATE = "completed"


class BambuddyError(ModuleError):
    """BamBuddy refused or failed (exit code 6, HTTP 502)."""


@dataclass(frozen=True)
class Printer:
    id: int
    name: str
    model: str
    is_active: bool
    nozzle_count: int = 1


@dataclass(frozen=True)
class PresetChoice:
    """Names of `standard`/`cloud`/`local` presets for one printer model."""

    printer: str
    process: str
    filament: str
    source: str = "standard"
    bed_type: str | None = None  # build plate installed on printers of this model

    def refs(self) -> dict[str, dict[str, str]]:
        return {
            "printer_preset": {"source": self.source, "id": self.printer},
            "process_preset": {"source": self.source, "id": self.process},
            "filament_preset": {"source": self.source, "id": self.filament},
        }


@dataclass(frozen=True)
class SliceResult:
    library_file_id: int
    name: str
    print_time_seconds: int | None
    filament_used_g: float | None


class BambuddyClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/") + "/api/v1",
            headers={"X-API-Key": api_key, "User-Agent": f"os2slice/{__version__}"},
            timeout=timeout,
            follow_redirects=False,
            transport=transport,
        )

    def __enter__(self) -> BambuddyClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # -- reads -------------------------------------------------------------

    def list_printers(self) -> list[Printer]:
        rows = self._json(self._request("GET", "/printers/"))
        if not isinstance(rows, list):
            raise BambuddyError("Unexpected printer list from BamBuddy")
        return [
            Printer(
                int(p["id"]),
                str(p.get("name", "")),
                str(p.get("model") or ""),
                bool(p.get("is_active")),
                int(p.get("nozzle_count") or 1),
            )
            for p in rows
            if isinstance(p, dict) and isinstance(p.get("id"), int)
        ]

    def filament_preset_names(self) -> list[str]:
        """Names of every filament preset BamBuddy can slice with (standard, cloud, local)."""
        return self.preset_names()["filament"]

    def preset_names(self) -> dict[str, list[str]]:
        """Printer, process and filament preset names, local then cloud then standard."""
        body = self._json(self._request("GET", "/slicer/presets"))
        names: dict[str, list[str]] = {"printer": [], "process": [], "filament": []}
        if not isinstance(body, dict):
            return names
        for tier in ("local", "cloud", "standard"):
            for kind, out in names.items():
                for item in (body.get(tier) or {}).get(kind) or []:
                    if isinstance(item, dict) and isinstance(item.get("name"), str):
                        out.append(item["name"])
        return names

    def download_file(self, file_id: int) -> bytes:
        return self._request("GET", f"/library/files/{int(file_id)}/download").content

    def auth_enabled(self) -> bool:
        body = self._json(self._request("GET", "/auth/status"))
        return bool(body.get("auth_enabled")) if isinstance(body, dict) else True

    def printer_status(self, printer_id: int) -> dict[str, Any]:
        body = self._json(self._request("GET", f"/printers/{int(printer_id)}/status"))
        return body if isinstance(body, dict) else {}

    # -- library -------------------------------------------------------------

    def ensure_folder(self, name: str, parent_id: int | None = None) -> int:
        # The endpoint returns a tree (root folders with nested `children`).
        pending = self._json(self._request("GET", "/library/folders/"))
        pending = list(pending) if isinstance(pending, list) else []
        while pending:
            f = pending.pop()
            if not isinstance(f, dict):
                continue
            if f.get("name") == name and f.get("parent_id") == parent_id:
                return int(f["id"])
            pending.extend(f.get("children") or [])
        body = {"name": name, "parent_id": parent_id}
        created = self._json(self._request("POST", "/library/folders/", json=body))
        return int(created["id"])

    def upload(self, folder_id: int, filename: str, data: bytes) -> int:
        r = self._request(
            "POST",
            "/library/files/",
            params={"folder_id": folder_id},
            files={"file": (filename, data, "application/octet-stream")},
        )
        return int(self._json(r)["id"])

    # -- slicing -------------------------------------------------------------

    def start_slice(
        self,
        file_id: int,
        presets: PresetChoice,
        settings: PrintSettings,
        auto_orient: bool = False,
        filament_colours: list[str] | None = None,
        bed_type: str | None = None,
        filament_presets: list[str] | None = None,
        extra_overrides: dict[str, Any] | None = None,
        auto_arrange: bool = True,
    ) -> int:
        body = {
            **presets.refs(),
            **(
                {
                    "filament_presets": [
                        {"source": presets.source, "id": n} for n in filament_presets
                    ]
                }
                if filament_presets
                else {}
            ),
            **({"bed_type": bed_type} if bed_type else {}),
            **({"filament_colours": filament_colours} if filament_colours else {}),
            "process_overrides": {**settings.process_overrides(), **(extra_overrides or {})},
            "export_3mf": True,
            "auto_orient": auto_orient,
            "auto_arrange": auto_arrange,
        }
        log.info("slice file %s: %s", file_id, body)
        r = self._request("POST", f"/library/files/{int(file_id)}/slice", json=body)
        return int(self._json(r)["job_id"])

    def wait_for_slice(
        self,
        job_id: int,
        timeout: float = 900.0,
        poll: float = 2.0,
        on_status: Callable[[str], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> SliceResult:
        deadline = time.monotonic() + timeout
        last = ""
        while True:
            job = self._json(self._request("GET", f"/slice-jobs/{int(job_id)}"))
            state = str(job.get("status", ""))
            if state != last and on_status:
                on_status(state)
            last = state
            if state == DONE_STATE:
                res = job.get("result") or {}
                if not isinstance(res.get("library_file_id"), int):
                    raise BambuddyError("Slice finished without a result file")
                return SliceResult(
                    library_file_id=res["library_file_id"],
                    name=str(res.get("name", "")),
                    print_time_seconds=res.get("print_time_seconds"),
                    filament_used_g=res.get("filament_used_g"),
                )
            if state in FAILED_STATES:
                detail = job.get("error_detail") or job.get("error") or job.get("message") or state
                raise BambuddyError(f"Slicing failed: {str(detail)[:300]}")
            if time.monotonic() > deadline:
                raise BambuddyError("Slicing took too long", "Check BamBuddy's slicer add-on")
            sleep(poll)

    # -- printing ------------------------------------------------------------

    def queue_print(
        self,
        library_file_id: int,
        printer_id: int,
        manual_start: bool,
        ams_mapping: list[int] | None = None,
        use_ams: bool | None = None,
    ) -> dict[str, Any]:
        """Queue a sliced file on one printer. The only call that can lead to a print."""
        body: dict[str, Any] = {
            "library_file_id": int(library_file_id),
            "printer_id": int(printer_id),
            "manual_start": bool(manual_start),
        }
        if ams_mapping is not None:
            body["ams_mapping"] = [int(x) for x in ams_mapping]
        if use_ams is not None:
            body["use_ams"] = bool(use_ams)
        log.warning("queueing on printer %s: %s", printer_id, body)
        item = self._json(self._request("POST", "/queue/", json=body))
        return item if isinstance(item, dict) else {}

    # -- plumbing ------------------------------------------------------------

    def _request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        try:
            r = self._client.request(method, path, **kw)
        except httpx.TimeoutException as e:
            raise BambuddyError("BamBuddy didn't answer in time") from e
        except httpx.TransportError as e:
            raise BambuddyError(
                f"Can't reach BamBuddy ({type(e).__name__})", "Check [bambuddy] base_url in config"
            ) from e
        log.debug("%s %s -> %s", method, r.request.url.copy_with(query=None), r.status_code)
        if r.status_code in (401, 403):
            raise AuthError(
                f"BamBuddy refused the API key ({r.status_code})",
                "Check the key and its permissions (read status, library, queue)",
            )
        if r.status_code == 429:
            raise BambuddyError("BamBuddy rate limit hit", "Wait a minute and try again")
        if not r.is_success:
            detail = ""
            try:
                body = r.json()
                detail = (
                    f": {str(body.get('detail', body))[:300]}" if isinstance(body, dict) else ""
                )
            except ValueError:
                pass
            raise BambuddyError(f"BamBuddy error {r.status_code} on {path}{detail}")
        return r

    @staticmethod
    def _json(r: httpx.Response) -> Any:
        try:
            return r.json()
        except ValueError as e:
            raise BambuddyError("BamBuddy returned invalid JSON") from e
