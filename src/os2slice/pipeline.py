"""ExportRequest → Onshape export → file → slicer → notification.

Shared by `send`, `handle` and (Phase 2) the localhost listener.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from os2slice import files, notify, slicers
from os2slice.auth import Keys, load_keys
from os2slice.config import Config, SlicerConfig
from os2slice.errors import BadRequest, Os2sliceError
from os2slice.logsetup import log_path, state_dir
from os2slice.onshape import OnshapeClient
from os2slice.request import ExportRequest

log = logging.getLogger(__name__)

Launcher = Callable[[SlicerConfig, Path, Path], None]


@dataclass(frozen=True)
class Result:
    ok: bool
    title: str
    detail: str
    exit_code: int = 0
    http_status: int = 200
    path: Path | None = None


def run(
    req: ExportRequest,
    cfg: Config,
    *,
    keys: Keys | None = None,
    client: OnshapeClient | None = None,
    launcher: Launcher = slicers.launch,
) -> Result:
    """Do the whole job. Raises Os2sliceError on any expected failure."""
    slicer = cfg.slicers.get(req.slicer)
    if slicer is None:  # parse_* already checked; guard anyway
        raise BadRequest(f"Slicer {req.slicer!r} isn't configured")
    if req.fmt != "stl":
        raise BadRequest(f"{req.fmt.upper()} export isn't implemented yet", "Use fmt=stl")
    if req.part_id is None:
        raise BadRequest("Whole Part Studio export isn't implemented yet", "Right-click a part")

    own_client = client is None
    if client is None:
        client = OnshapeClient(cfg.onshape_base_url, keys or load_keys())
    try:
        doc_name = client.get_document_name(req.document_id)
        part_name = client.get_part_name(req)
        data = client.export_stl(req, units=cfg.units)
    finally:
        if own_client:
            client.close()

    target = files.export_path(cfg.export_dir, doc_name, part_name, req.configuration, req.fmt)
    path = files.write_export(target, data, cfg.export_dir)
    log.info("exported %r (%d bytes) to %s", part_name, len(data), path)

    try:
        removed = files.prune(cfg.export_dir, cfg.keep_days)
        if removed:
            log.info("pruned %d old export(s)", removed)
    except OSError as e:
        log.warning("prune failed: %s", e)

    launcher(slicer, path, state_dir() / "slicers.log")
    return Result(
        ok=True,
        title=f"Sent {part_name} to {slicer.name}",
        detail=f"Saved {path}",
        path=path,
    )


def run_and_report(
    make_request: Callable[[], ExportRequest],
    cfg: Config,
    *,
    keys: Keys | None = None,
    client: OnshapeClient | None = None,
    launcher: Launcher = slicers.launch,
) -> Result:
    """Run the pipeline and turn every outcome into a logged, notified Result. Never raises."""
    try:
        req = make_request()
        log.info("request: %s", req)
        result = run(req, cfg, keys=keys, client=client, launcher=launcher)
    except Os2sliceError as e:
        log.error("%s", e.one_line(), exc_info=True)
        result = Result(
            ok=False,
            title="os2slice: " + e.message,
            detail=e.fix,
            exit_code=e.exit_code,
            http_status=e.http_status,
        )
    except Exception:
        log.exception("unexpected error")
        result = Result(
            ok=False,
            title="os2slice: unexpected error",
            detail=f"See {log_path()}",
            exit_code=1,
            http_status=500,
        )
    notify.notify(result.title, result.detail, error=not result.ok)
    return result
