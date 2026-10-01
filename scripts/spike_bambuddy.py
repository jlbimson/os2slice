# /// script
# requires-python = ">=3.10"
# dependencies = ["httpx"]
# ///
"""Phase 2 spike: upload an STL to BamBuddy, slice it with overrides, check the result.

    set -a; source ~/.config/os2slice/bambuddy.env; set +a
    uv run scripts/spike_bambuddy.py scripts/spike_JHD_cfg.stl \
        --overrides '{"wall_loops": 3, "sparse_infill_density": "25%"}'

Prints every raw response so the shapes can go in docs/BAMBUDDY_API.md.
Nothing is queued or printed unless --really-print PRINTER_ID is given (D-13),
and even then the queue item is created with manual_start so it waits.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
import zipfile
from pathlib import Path
from typing import Any

import httpx

FOLDER = "os2slice-spike"


def show(label: str, r: httpx.Response) -> Any:
    print(f"\n{label}: {r.request.method} {r.request.url.path} -> {r.status_code}")
    try:
        body = r.json()
    except ValueError:
        print(f"  ({r.headers.get('content-type')}, {len(r.content)} bytes)")
        return None
    print("  " + json.dumps(body, indent=2)[:3000].replace("\n", "\n  "))
    return body


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("stl", type=Path)
    ap.add_argument("--printer-preset", default="Bambu Lab A1 mini 0.4 nozzle")
    ap.add_argument("--process-preset", default="0.20mm Standard @BBL A1M")
    ap.add_argument("--filament-preset", default="Bambu PLA Basic @BBL A1M")
    ap.add_argument("--overrides", default="{}", help="JSON object for process_overrides")
    ap.add_argument("--really-print", type=int, metavar="PRINTER_ID")
    args = ap.parse_args()

    base = os.environ["BAMBUDDY_URL"].rstrip("/") + "/api/v1"
    headers = {"X-API-Key": os.environ["BAMBUDDY_API_KEY"], "Accept": "application/json"}
    overrides = json.loads(args.overrides)

    with httpx.Client(base_url=base, headers=headers, timeout=60) as c:
        folders = c.get("/library/folders/").json()
        folder = next((f for f in folders if f.get("name") == FOLDER), None)
        if folder is None:
            folder = show("create folder", c.post("/library/folders/", json={"name": FOLDER}))
        folder_id = folder["id"]
        print(f"folder {FOLDER!r} id={folder_id}")

        with args.stl.open("rb") as fh:
            r = c.post(
                "/library/files/",
                params={"folder_id": folder_id},
                files={"file": (args.stl.name, fh, "application/octet-stream")},
            )
        uploaded = show("upload", r)
        r.raise_for_status()
        file_id = uploaded.get("id") or uploaded.get("file_id") or uploaded["file"]["id"]

        def ref(name: str) -> dict[str, str]:
            return {"source": "standard", "id": name}

        body = {
            "printer_preset": ref(args.printer_preset),
            "process_preset": ref(args.process_preset),
            "filament_preset": ref(args.filament_preset),
            "process_overrides": overrides or None,
            "export_3mf": True,
            "auto_orient": False,
            "auto_arrange": True,
        }
        print("\nslice request:", json.dumps(body))
        job = show("slice", c.post(f"/library/files/{file_id}/slice", json=body))
        job_id = job.get("job_id") or job.get("id")

        status: dict[str, Any] = {}
        for _ in range(180):
            status = c.get(f"/slice-jobs/{job_id}").json()
            state = status.get("status") or status.get("state")
            print(f"  job {job_id}: {state} {status.get('progress', '')}")
            if state in ("completed", "done", "failed", "error", "cancelled"):
                break
            time.sleep(2)
        show("final job", c.get(f"/slice-jobs/{job_id}"))

        result_id = (status.get("result") or {}).get("library_file_id") or status.get(
            "result_file_id"
        )
        if not result_id:
            print("\nNo result file id found in the job; inspect the output above.")
            return 1
        show("result file", c.get(f"/library/files/{result_id}"))
        r = c.get(f"/library/files/{result_id}/download")
        print(f"\ndownload -> {r.status_code}, {len(r.content)} bytes")
        check_settings(r.content, overrides)

        if args.really_print is not None:
            item = {
                "printer_id": args.really_print,
                "library_file_id": result_id,
                "manual_start": True,
            }
            show("queue (manual_start)", c.post("/queue/", json=item))
    return 0


def check_settings(data: bytes, overrides: dict[str, Any]) -> None:
    """Look inside the sliced .gcode.3mf for the settings actually used."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        print("result isn't a zip/3mf")
        return
    print("3mf entries:", [n for n in z.namelist() if not n.endswith(".png")][:20])
    keys = [
        "wall_loops",
        "sparse_infill_density",
        "enable_support",
        "support_type",
        "support_on_build_plate_only",
        "layer_height",
        "printer_settings_id",
        "print_settings_id",
        "filament_settings_id",
        *overrides,
    ]
    for name in z.namelist():
        if name.endswith("project_settings.config"):
            cfg = json.loads(z.read(name))
            print(f"\n{name}:")
            for k in dict.fromkeys(keys):
                print(f"  {k} = {cfg.get(k)!r}")


if __name__ == "__main__":
    sys.exit(main())
