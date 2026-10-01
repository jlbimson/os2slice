"""Slicer module for the OrcaSlicer / Bambu Studio REST sidecars (docs/SLICERAPI_API.md).

Two upstreams share one REST shape (multipart `POST /slice`, `GET /health`):

- **afk**: `afkfelix/orca-slicer-api` (image `ghcr.io/afkfelix/orca-slicer-api`). System
  profiles by name (`GET /profiles/{printers,presets,filaments}`), a working async API
  (`/slice-async`), one filament per request.
- **resolver**: `maziggy/orca-slicer-api` branch `bambuddy/profile-resolver`, which
  BamBuddy's "Bambu Studio API" add-on packages. Names in the `printer`/`preset`/`filament`
  form fields mean *stored user* profiles there, so system presets go as uploaded stubs
  (`{name, inherits, from, type}`) that its resolver flattens; it lists system presets at
  `GET /profiles/bundled`, takes up to 16 filament uploads on `POST /slice`, and reports
  progress at `GET /slice/progress/{requestId}`. Its `/slice-async` drops uploaded
  filament profiles, so on this flavour the module uses `POST /slice` + progress polling,
  as BamBuddy does.

Both flavours get the same profile stubs (with `resolveProfileInheritance=true` for afk)
and the process overrides written into the process stub. The flavour is probed once
(`GET /profiles/bundled`). Only the configured URL is called; ids from responses are
validated and URLs are built locally, never followed.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import threading
import time
import uuid
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, Literal
from urllib.parse import quote, urlsplit

import httpx

from os2slice.modules.base import (
    MEDIA_GCODE,
    MEDIA_GCODE_3MF,
    Field,
    Health,
    Material,
    Media,
    ModuleError,
    ModuleSpec,
    PartGeometry,
    ProfileCatalog,
    Progress,
    SliceInput,
    SliceOutput,
)
from os2slice.orientation import bounding_box, translate_xy
from os2slice.threemf import Part, build_3mf

Flavour = Literal["afk", "resolver"]

# Copies (D-25): gap between neighbours, more with a brim, and a margin around the bed, mm.
# TODO(9a merge): use bambu_project.copy_offsets (and its constants) instead of _grid.
COPY_GAP, BRIM_GAP, BED_MARGIN = 6.0, 10.0, 5.0
DEFAULT_BED = (256.0, 256.0)  # when the printer doesn't say (the core's fallback too)

REQUEST_ID_RE = re.compile(r"^[0-9A-Za-z-]{8,64}$")
OVERRIDE_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
TOTAL_LAYERS_RE = (
    re.compile(rb"; total layer number: (\d+)"),
    re.compile(rb"; total layers count = (\d+)"),
    re.compile(rb"; layer num/total_layer_count: \d+/(\d+)"),
)
TIME_RE = re.compile(
    rb"(?:total estimated time|estimated printing time \(normal mode\))\s*[:=]\s*"
    rb"(?:(\d+)d\s*)?(?:(\d+)h\s*)?(?:(\d+)m\s*)?(?:(\d+)s)?"
)
GRAMS_RE = (  # one value per filament, comma-separated
    re.compile(rb"; total filament weight \[g\]\s*:\s*([\d.,]+)"),
    re.compile(rb"; filament used \[g\]\s*=\s*([\d.,]+)"),
)
SLICE_INFO = "Metadata/slice_info.config"


def _fields(port: int) -> tuple[Field, ...]:
    return (
        Field(
            "url",
            "Sidecar URL",
            "url",
            required=True,
            help=f"Base URL of the sidecar, e.g. http://172.30.32.1:{port}",
        ),
        Field(
            "timeout_s",
            "Slice timeout (s)",
            "int",
            default=900,
            help="Give up on a slice that hasn't finished after this many seconds.",
        ),
        Field(
            "api_key",
            "API key",
            "secret",
            help="The sidecar has no auth of its own. Only for a reverse proxy in front "
            "of it that wants `Authorization: Bearer <key>`; leave empty otherwise.",
        ),
    )


ORCA_SPEC = ModuleSpec(
    kind="orca-slicer-api",
    label="OrcaSlicer API sidecar",
    role="slicer",
    technology="fdm",
    fields=_fields(3003),
    makes=(MEDIA_GCODE_3MF, MEDIA_GCODE),
    help="Headless OrcaSlicer behind a REST API (afkfelix/orca-slicer-api, or the "
    "maziggy fork BamBuddy uses; default port 3003 next to BamBuddy).",
)
BAMBU_SPEC = ModuleSpec(
    kind="bambu-studio-api",
    label="Bambu Studio API sidecar",
    role="slicer",
    technology="fdm",
    fields=_fields(3001),
    makes=(MEDIA_GCODE_3MF, MEDIA_GCODE),
    help="Headless Bambu Studio behind the same REST API (BamBuddy's 'Bambu Studio API' "
    "Home Assistant add-on, default port 3001).",
)
SPECS: tuple[ModuleSpec, ...] = (ORCA_SPEC, BAMBU_SPEC)


@dataclass(frozen=True)
class _Filament:
    profile: str
    colour: str | None


@dataclass(frozen=True)
class _Stats:
    print_time_s: int | None = None
    material_g: float | None = None


class SlicerApi:
    """One sidecar. Use `OrcaSlicerApi` / `BambuStudioApi` (they differ only in `spec`)."""

    spec: ClassVar[ModuleSpec] = ORCA_SPEC
    poll_s: float = 1.0  # status / progress polling interval

    def __init__(
        self,
        url: str,
        timeout_s: float = 900,
        api_key: str = "",
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        parts = urlsplit(url.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ModuleError(f"{self.spec.label}: {url[:80]!r} isn't an http(s) URL")
        if parts.query or parts.fragment or parts.username or parts.password:
            raise ModuleError(f"{self.spec.label}: the URL may not carry a query or credentials")
        if timeout_s <= 0:
            raise ModuleError(f"{self.spec.label}: timeout_s must be positive")
        self.url = url.strip().rstrip("/")
        self.timeout_s = float(timeout_s)
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._transport = transport
        self._client = self._new_client()
        self._flavour: Flavour | None = None

    @classmethod
    def from_values(
        cls, values: Mapping[str, Any], *, transport: httpx.BaseTransport | None = None
    ) -> SlicerApi:
        """The registry factory: validated field values (secret already resolved)."""
        return cls(
            str(values["url"]),
            values.get("timeout_s") or 900,
            str(values.get("api_key") or ""),
            transport=transport,
        )

    def _new_client(self, read: float = 30.0) -> httpx.Client:
        return httpx.Client(
            base_url=self.url,
            headers=self._headers,
            timeout=httpx.Timeout(10.0, read=read, write=60.0),
            transport=self._transport,
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> SlicerApi:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- HTTP helpers -------------------------------------------------------------

    def _request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        try:
            return self._client.request(method, path, **kw)
        except httpx.TimeoutException as e:
            raise ModuleError(
                f"The slicer at {self.url} didn't answer in time ({method} {path})",
                "Check that the sidecar is running and not overloaded",
            ) from e
        except httpx.RequestError as e:
            raise ModuleError(
                f"Can't reach the slicer at {self.url}: {e or type(e).__name__}",
                "Check the sidecar URL and that the add-on / container is running",
            ) from e

    def _json(self, resp: httpx.Response, what: str) -> Any:
        if resp.status_code >= 400:
            raise _refused(resp, what)
        try:
            return resp.json()
        except ValueError as e:
            raise ModuleError(f"The slicer's {what} answer isn't JSON") from e

    def flavour(self) -> Flavour:
        """Which upstream this is: `resolver` answers `GET /profiles/bundled`, afk doesn't."""
        if self._flavour is None:
            resp = self._request("GET", "/profiles/bundled")
            body: Any = None
            if resp.status_code == 200:
                try:
                    body = resp.json()
                except ValueError:
                    body = None
            self._flavour = "resolver" if isinstance(body, dict) else "afk"
        return self._flavour

    # -- Slicer protocol ----------------------------------------------------------

    def check(self) -> Health:
        try:
            resp = self._request("GET", "/health")
        except ModuleError as e:
            return Health(False, e.message, detail=e.fix)
        try:
            body = resp.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return Health(False, f"{self.url}/health answered HTTP {resp.status_code}, not JSON")
        checks = body.get("checks") if isinstance(body.get("checks"), dict) else {}
        slicer = checks.get("orcaslicer") if isinstance(checks.get("orcaslicer"), dict) else {}
        version = str(slicer.get("version") or "")
        problems = [
            f"{name}: {c.get('error')}"
            for name, c in checks.items()
            if isinstance(c, dict) and c.get("error")
        ]
        ok = resp.status_code == 200 and body.get("status") == "healthy"
        summary = f"{self.spec.label} {'healthy' if ok else 'unhealthy'}"
        if version and version != "unknown":
            summary += f", slicer {version}"
        if problems:
            summary += f" ({'; '.join(problems)[:200]})"
        return Health(ok, summary, version, json.dumps(body)[:1000])

    def profiles(self, printer_model: str = "") -> ProfileCatalog:
        if self.flavour() == "resolver":
            body = self._json(self._request("GET", "/profiles/bundled"), "profile list")
            printers = _entries(body.get("printer"))
            process = _entries(body.get("process"))
            filament = _entries(body.get("filament"))
        else:
            printers, process, filament = (
                [(n, None) for n in self._names(cat)]
                for cat in ("printers", "presets", "filaments")
            )
        if not printer_model:
            return ProfileCatalog(
                tuple(n for n, _ in printers),
                tuple(n for n, _ in process),
                tuple(n for n, _ in filament),
            )
        wanted = _norm(printer_model)
        chosen = {n for n, _ in printers if n == printer_model or wanted in _norm(n)}

        def fits(compat: list[str] | None) -> bool:
            # No compatibility data (afk lists names only): keep it rather than guess
            # from the name ("0.20mm Standard @BBL A1M" never says "A1 mini").
            return compat is None or bool(chosen.intersection(compat))

        return ProfileCatalog(
            tuple(sorted(chosen)),
            tuple(n for n, c in process if fits(c)),
            tuple(n for n, c in filament if fits(c)),
        )

    def _names(self, category: str) -> list[str]:
        body = self._json(self._request("GET", f"/profiles/{category}"), f"{category} list")
        if not isinstance(body, list):
            raise ModuleError(f"The slicer's {category} list isn't a list")
        return [str(n) for n in body if isinstance(n, str)]

    def slice(self, job: SliceInput, progress: Progress) -> SliceOutput:
        media: Media = job.media or MEDIA_GCODE_3MF
        if media not in self.spec.makes:
            raise ModuleError(f"{self.spec.label} can't make {media} files")
        if not job.parts:
            raise ModuleError("Nothing to slice: the job has no parts")
        if not (job.profiles.printer and job.profiles.process):
            raise ModuleError(
                f"No printer/process profile for {job.printer.name}",
                "Set the printer's profiles in the config",
            )
        flavour = self.flavour()
        model_name, model, filaments = self._model(job)
        if flavour == "afk" and len(filaments) > 1:
            raise ModuleError(
                "This OrcaSlicer API sidecar takes one filament per slice",
                "Print multi-material parts through the bambu-studio-api (BamBuddy) sidecar",
            )
        model_type = "model/3mf" if model_name.endswith(".3mf") else "model/stl"
        # extra["process_overrides"]: what the core adds, e.g. the prime tower spot that
        # multi-filament plates need (D-20; without one the slicer refuses with "G-code
        # outside the printable area"). TODO(9a merge): bambu_project.tower_spots + retry.
        extra = job.extra.get("process_overrides")
        overrides = {
            **job.settings.process_overrides(),
            **(extra if isinstance(extra, Mapping) else {}),
        }
        printer = _stub(job.profiles.printer, "machine")
        process = _stub(job.profiles.process, "process", overrides)
        files: list[tuple[str, tuple[str, bytes, str]]] = [
            ("file", (model_name, model, model_type)),
            _json_part("printerProfile", "printer.json", printer),
            _json_part("presetProfile", "preset.json", process),
        ]
        for n, f in enumerate(filaments, start=1):
            colour = {"filament_colour": [f.colour]} if f.colour else {}
            stub = _stub(f.profile, "filament", colour)
            files.append(_json_part("filamentProfile", f"filament_{n}.json", stub))
        data = {"plate": "1"}
        if media == MEDIA_GCODE_3MF:
            data["exportType"] = "3mf"
        # Multipart booleans arrive as strings and "false" is truthy upstream: omit = off.
        if job.auto_arrange:
            data["arrange"] = "true"
        if job.auto_orient:
            data["orient"] = "true"
        if job.bed_type:
            data["bedType"] = job.bed_type
        if flavour == "afk":
            data["resolveProfileInheritance"] = "true"
            try:
                out, stats, rid = self._slice_async(files, data, progress)
            except ModuleError as e:
                raise self._explain(e, job, filaments) from e
        else:
            out, stats, rid = self._slice_sync(files, data, progress)
        return self._output(job, media, out, stats, flavour, rid, model_name, filaments)

    # -- input --------------------------------------------------------------------

    def _model(self, job: SliceInput) -> tuple[str, bytes, list[_Filament]]:
        """The model file to upload and the filaments in slot order."""
        stem = re.sub(r"[^A-Za-z0-9._-]", "_", job.job_name)[:60].strip("._") or "model"
        pinned = job.printer.nozzle_count > 1 and any(
            p.material is not None and p.material.extruder is not None for p in job.parts
        )
        bed = job.printer.bed_mm or DEFAULT_BED
        if len(job.parts) == 1 and job.copies == 1 and not pinned:
            part = job.parts[0]
            stl = part.stl
            if not job.auto_arrange and job.printer.bed_mm:
                stl = _centred([stl], bed)[0]
            return f"{stem}.stl", stl, [_filament(part.material, job.profiles.filament)]
        # Several parts, copies or a pinned nozzle: one Bambu-style 3MF object (D-20, D-25).
        slots: list[str] = []
        filaments: list[_Filament] = []
        for p in job.parts:
            key = p.material.id if p.material else ""
            if key not in slots:
                slots.append(key)
                filaments.append(_filament(p.material, job.profiles.filament))
        placed = _centred([p.stl for p in job.parts], bed)
        lo, hi = _box(placed)
        gap = COPY_GAP + (BRIM_GAP if job.settings.brim else 0.0)
        offsets = _grid((hi[0] - lo[0], hi[1] - lo[1]), job.copies, bed, gap)
        maps = None
        if pinned:
            mats = [_material_of(job.parts, k) for k in slots]
            maps = [1 if m is not None and m.extruder == 1 else 2 for m in mats]
        parts = [
            Part(p.name, stl, slots.index(p.material.id if p.material else "") + 1)
            for p, stl in zip(job.parts, placed, strict=True)
        ]
        return f"{stem}.3mf", build_3mf(parts, job.job_name, maps, offsets), filaments

    # -- slicing ------------------------------------------------------------------

    def _slice_async(
        self, files: list[Any], data: dict[str, str], progress: Progress
    ) -> tuple[bytes, _Stats, str]:
        """afk: POST /slice-async → poll GET /slice-async/{id} → GET …/result → DELETE."""
        deadline = time.monotonic() + self.timeout_s
        progress("Uploading to the slicer")
        resp = self._request("POST", "/slice-async", data=data, files=files)
        body = self._json(resp, "slice request")
        rid = str(body.get("requestId", "")) if isinstance(body, dict) else ""
        if not REQUEST_ID_RE.fullmatch(rid):
            raise ModuleError("The slicer didn't return a usable request id")
        last = ""
        while True:
            status_body = self._json(self._request("GET", f"/slice-async/{rid}"), "slice status")
            status = str(status_body.get("status", "")) if isinstance(status_body, dict) else ""
            if status == "completed":
                break
            if status == "failed":
                reason = str(status_body.get("message") or "no reason given")[:500]
                prefix = "" if reason.lower().startswith("slicing failed") else "Slicing failed: "
                raise ModuleError(prefix + reason)
            if status not in ("pending", "processing"):
                raise ModuleError(f"The slicer reported an unknown job state {status[:40]!r}")
            if status != last:
                progress(f"Slicing ({status})")
                last = status
            if time.monotonic() >= deadline:
                raise _timed_out(self.timeout_s)
            time.sleep(self.poll_s)
        meta = status_body.get("metadata") if isinstance(status_body.get("metadata"), dict) else {}
        progress("Downloading the sliced file")
        with self._new_client(read=max(60.0, self.timeout_s)) as client:
            try:
                result = client.get(f"/slice-async/{rid}/result")
            except httpx.RequestError as e:
                raise ModuleError(
                    f"Couldn't download the sliced file: {e or type(e).__name__}"
                ) from e
        if result.status_code >= 400:
            raise _refused(result, "sliced file")
        with contextlib.suppress(httpx.RequestError):
            self._client.delete(f"/slice-async/{rid}")  # tidy the sidecar; best effort
        stats = _Stats(
            _pos_int(meta.get("printTime")) or _pos_int(result.headers.get("x-print-time-seconds")),
            _pos_float(meta.get("filamentUsedG"))
            or _pos_float(result.headers.get("x-filament-used-g")),
        )
        return result.content, stats, rid

    def _slice_sync(
        self, files: list[Any], data: dict[str, str], progress: Progress
    ) -> tuple[bytes, _Stats, str]:
        """resolver: POST /slice (held open) while polling GET /slice/progress/{id}."""
        rid = str(uuid.uuid4())
        data = {**data, "requestId": rid}
        deadline = time.monotonic() + self.timeout_s
        box: dict[str, Any] = {}

        def post() -> None:
            try:
                with self._new_client(read=self.timeout_s + 5) as client:
                    box["resp"] = client.post("/slice", data=data, files=files)
            except Exception as e:  # handed to the caller's thread
                box["error"] = e

        progress("Uploading to the slicer")
        worker = threading.Thread(target=post, name=f"slicerapi-{rid[:8]}", daemon=True)
        worker.start()
        last = ""
        while True:
            worker.join(self.poll_s)
            if not worker.is_alive():
                break
            if time.monotonic() >= deadline:
                raise _timed_out(self.timeout_s)
            line = self._progress_line(rid)
            if line and line != last:
                progress(line)
                last = line
        error = box.get("error")
        if isinstance(error, httpx.TimeoutException):
            raise _timed_out(self.timeout_s) from error
        if isinstance(error, httpx.RequestError):
            raise ModuleError(
                f"Lost the slicer at {self.url}: {error or type(error).__name__}"
            ) from error
        if error is not None:
            raise ModuleError(f"Slicing request failed: {error}") from error
        resp: httpx.Response = box["resp"]
        if resp.status_code >= 400:
            raise _refused(resp, "slice request")
        stats = _Stats(
            _pos_int(resp.headers.get("x-print-time-seconds")),
            _pos_float(resp.headers.get("x-filament-used-g")),
        )
        return resp.content, stats, rid

    def _explain(
        self, error: ModuleError, job: SliceInput, filaments: list[_Filament]
    ) -> ModuleError:
        """afk says only "Failed to prepare slicing" for an unknown profile: name it."""
        if "prepare slicing" not in error.message:
            return error
        wanted = [("printers", job.profiles.printer), ("presets", job.profiles.process)]
        wanted += [("filaments", f.profile) for f in filaments]
        unknown = []
        for category, name in wanted:
            try:
                resp = self._client.get(f"/profiles/{category}/{quote(name, safe='')}")
            except httpx.RequestError:
                return error
            if resp.status_code == 404:
                unknown.append(name)
        if not unknown:
            return error
        return ModuleError(
            f"{error.message}: the slicer has no profile named {', '.join(map(repr, unknown))}",
            "Pick profile names from the slicer's own list",
        )

    def _progress_line(self, rid: str) -> str:
        try:
            resp = self._client.get(f"/slice/progress/{rid}", timeout=5.0)
            body = resp.json() if resp.status_code == 200 else None
        except (httpx.RequestError, ValueError):
            return ""  # progress is best effort; 404 before the slice starts is normal
        if not isinstance(body, dict):
            return ""
        stage = str(body.get("stage") or "").strip()[:80]
        pct = _pos_int(body.get("total_percent"))
        if not stage and pct is None:
            return ""
        return f"Slicing: {stage or 'working'}" + (f" ({pct}%)" if pct is not None else "")

    # -- output -------------------------------------------------------------------

    def _output(
        self,
        job: SliceInput,
        media: Media,
        data: bytes,
        stats: _Stats,
        flavour: Flavour,
        rid: str,
        model_name: str,
        filaments: list[_Filament],
    ) -> SliceOutput:
        if not data:
            raise ModuleError("The slicer returned an empty file")
        is_zip = zipfile.is_zipfile(io.BytesIO(data))
        gcode = b""
        info = b""
        if media == MEDIA_GCODE_3MF:
            if not is_zip:
                raise ModuleError(
                    f"The slicer's answer isn't a 3MF ({len(data)} bytes)",
                    "Check the sidecar URL and any proxy in front of it",
                )
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = z.namelist()
                plates = [
                    n for n in names if n.startswith("Metadata/plate_") and n.endswith(".gcode")
                ]
                if not plates:
                    raise ModuleError("The sliced 3MF has no G-code in it")
                gcode = z.read(plates[0])
                info = z.read(SLICE_INFO) if SLICE_INFO in names else b""
        else:
            if is_zip:
                raise ModuleError(
                    "The slicer sent several plates (a ZIP) where one G-code was expected"
                )
            gcode = data
        # The file first: the sidecar's X-Filament-Used-g header is only the first
        # filament's weight on multi-filament slices (docs/SLICERAPI_API.md).
        time_s = _info_value(info, "prediction", int) or _gcode_time(gcode) or stats.print_time_s
        grams = _info_value(info, "weight", float) or _gcode_grams(gcode) or stats.material_g
        ext = "gcode.3mf" if media == MEDIA_GCODE_3MF else "gcode"
        return SliceOutput(
            data=data,
            filename=f"{job.job_name}.{ext}",
            media=media,
            print_time_s=time_s,
            material_g=grams,
            layers=_gcode_layers(gcode),
            report={
                "slicer": self.spec.kind,
                "flavour": flavour,
                "request_id": rid,
                "input": model_name.rsplit(".", 1)[-1],
                "filaments": [f.profile for f in filaments],
            },
        )


class OrcaSlicerApi(SlicerApi):
    spec: ClassVar[ModuleSpec] = ORCA_SPEC


class BambuStudioApi(SlicerApi):
    spec: ClassVar[ModuleSpec] = BAMBU_SPEC


MODULES: dict[str, type[SlicerApi]] = {
    ORCA_SPEC.kind: OrcaSlicerApi,
    BAMBU_SPEC.kind: BambuStudioApi,
}


# -- helpers ------------------------------------------------------------------------


def _stub(name: str, kind: str, extra: Mapping[str, Any] | None = None) -> bytes:
    """A profile that inherits everything from the system preset `name`, plus `extra`.

    The resolver fork flattens it against the slicer's bundled profiles (as BamBuddy's
    "standard" presets do); afk does the same with resolveProfileInheritance=true.
    Values are written the way presets store them: strings ("1" for true).
    """
    body: dict[str, Any] = {}
    for key, value in (extra or {}).items():
        if not OVERRIDE_KEY_RE.fullmatch(key):
            raise ModuleError(f"Unusable slicer setting name {key[:40]!r}")
        body[key] = [_setting(v) for v in value] if isinstance(value, list) else _setting(value)
    body.update({"name": name, "inherits": name, "from": "system", "type": kind})
    return json.dumps(body).encode()


def _json_part(field: str, filename: str, body: bytes) -> tuple[str, tuple[str, bytes, str]]:
    return field, (filename, body, "application/json")  # upstream checks type and .json


def _setting(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float, str)):
        return str(value)
    raise ModuleError(f"Unusable slicer setting value {value!r:.40}")


def _filament(material: Material | None, default: str) -> _Filament:
    profile = (material.profile if material else "") or default
    if not profile:
        raise ModuleError(
            "No filament profile for this print", "Set the printer's filament profile"
        )
    return _Filament(profile, material.colour if material else None)


def _material_of(parts: tuple[PartGeometry, ...], key: str) -> Material | None:
    return next((p.material for p in parts if (p.material.id if p.material else "") == key), None)


def _box(stls: list[bytes]) -> tuple[tuple[float, float], tuple[float, float]]:
    boxes = [bounding_box(s) for s in stls]
    lo = (min(b[0][0] for b in boxes), min(b[0][1] for b in boxes))
    hi = (max(b[1][0] for b in boxes), max(b[1][1] for b in boxes))
    return lo, hi


def _centred(stls: list[bytes], bed: tuple[float, float]) -> list[bytes]:
    """Move the parts together so their joint footprint is centred on the bed."""
    lo, hi = _box(stls)
    dx, dy = bed[0] / 2 - (lo[0] + hi[0]) / 2, bed[1] / 2 - (lo[1] + hi[1]) / 2
    return [translate_xy(s, dx, dy) for s in stls]


def _grid(
    size: tuple[float, float], copies: int, bed: tuple[float, float], gap: float
) -> list[tuple[float, float]]:
    """X/Y offsets for `copies` of a (w, d) footprint: the squarest grid that fits the
    bed (less a margin), centred on the original position. Refuses when none fits.
    TODO(9a merge): use bambu_project.copy_offsets.
    """
    w, d = size
    room_w, room_d = bed[0] - 2 * BED_MARGIN, bed[1] - 2 * BED_MARGIN
    best: tuple[float, int, int] | None = None
    for cols in range(1, copies + 1):
        rows = -(-copies // cols)
        grid_w, grid_d = cols * w + (cols - 1) * gap, rows * d + (rows - 1) * gap
        if grid_w <= room_w and grid_d <= room_d:
            score = max(grid_w / room_w, grid_d / room_d)
            if best is None or score < best[0]:
                best = (score, cols, rows)
    if best is None:
        raise ModuleError(
            f"{copies} {'copies' if copies > 1 else 'copy'} don't fit on the plate",
            f"Each copy is {w:.0f} x {d:.0f} mm on a {bed[0]:.0f} x {bed[1]:.0f} mm bed; "
            "print fewer copies",
        )
    _, cols, rows = best
    pitch_x, pitch_y = w + gap, d + gap
    x0, y0 = -(cols - 1) * pitch_x / 2, -(rows - 1) * pitch_y / 2
    return [(x0 + (n % cols) * pitch_x, y0 + (n // cols) * pitch_y) for n in range(copies)]


def _entries(raw: Any) -> list[tuple[str, list[str] | None]]:
    """`/profiles/bundled` items: (name, compatible_printers or None)."""
    out: list[tuple[str, list[str] | None]] = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            compat = item.get("compatible_printers")
            out.append(
                (item["name"], [str(c) for c in compat] if isinstance(compat, list) else None)
            )
    return out


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _refused(resp: httpx.Response, what: str) -> ModuleError:
    """The sidecar's own reason: `{"message", "details"?}` (details = the CLI's stderr)."""
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and (body.get("message") or body.get("details")):
        reason = str(body.get("message") or "")
        details = str(body.get("details") or "")
        # The CLI's log is mostly [trace] noise; its [error] lines are the reason.
        errors = [ln.split("]")[-1].strip() for ln in details.splitlines() if "[error]" in ln]
        details = "; ".join(errors) if errors else details
        if details:
            reason = f"{reason}: {details}" if reason else details
    else:
        reason = resp.text.strip() or resp.reason_phrase
    fix = "Check the profile names" if resp.status_code == 404 else ""
    return ModuleError(
        f"The slicer refused the {what} (HTTP {resp.status_code}): {reason[:600]}", fix
    )


def _timed_out(timeout_s: float) -> ModuleError:
    return ModuleError(
        f"Slicing didn't finish within {timeout_s:.0f} s",
        "Raise timeout_s for this slicer, or check the sidecar's log",
    )


def _pos_int(value: Any) -> int | None:
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _pos_float(value: Any) -> float | None:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _info_value(info: bytes, key: str, cast: type[int] | type[float]) -> Any:
    """`<metadata key="prediction" value="1719"/>` from Metadata/slice_info.config."""
    m = re.search(rb'<metadata key="' + key.encode() + rb'" value="([\d.]+)"', info)
    return (_pos_int if cast is int else _pos_float)(m.group(1).decode()) if m else None


def _gcode_time(gcode: bytes) -> int | None:
    m = TIME_RE.search(gcode[:200_000])
    if not m or not any(m.groups()):
        return None
    d, h, mi, s = (int(g or 0) for g in m.groups())
    return (d * 86400 + h * 3600 + mi * 60 + s) or None


def _gcode_grams(gcode: bytes) -> float | None:
    for pattern in GRAMS_RE:
        m = pattern.search(gcode[:200_000]) or pattern.search(gcode[-200_000:])
        if m:
            values = [_pos_float(v) for v in m.group(1).decode().split(",")]
            return round(sum(v for v in values if v), 2) or None
    return None


def _gcode_layers(gcode: bytes) -> int | None:
    for pattern in TOTAL_LAYERS_RE:
        m = pattern.search(gcode[:200_000])
        if m:
            return _pos_int(m.group(1).decode())
    return None
