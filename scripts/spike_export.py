# /// script
# requires-python = ">=3.10"
# dependencies = ["httpx"]
# ///
"""Phase 0 spike: list the parts in a Part Studio and export one as binary STL.

Usage (keys from env, never on the command line):

    export ONSHAPE_ACCESS_KEY=... ONSHAPE_SECRET_KEY=...
    uv run scripts/spike_export.py '<part studio URL>' [partId] [configuration]

Without a partId it just prints the part list. Prints request shapes, status
codes and redirect hops (never headers) so the findings can go in
docs/ONSHAPE_API.md.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import httpx

BASE = "https://cad.onshape.com"
ONSHAPE_HOST_RE = re.compile(r"^[a-z0-9-]+\.onshape\.com$")
URL_RE = re.compile(
    r"^https://[a-z0-9-]+\.onshape\.com/documents/([0-9a-f]{24})/([wvm])/([0-9a-f]{24})/e/([0-9a-f]{24})"
)


def log_response(resp: httpx.Response) -> None:
    for hop in resp.history:
        print(
            f"  {hop.request.method} {hop.request.url.copy_with(query=None)} -> {hop.status_code}"
        )
    print(
        f"  {resp.request.method} {resp.request.url.copy_with(query=None)} -> {resp.status_code}"
        f" ({resp.headers.get('content-type')}, {len(resp.content)} bytes)"
    )


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    m = URL_RE.match(sys.argv[1])
    if not m:
        print("Not a Part Studio URL: expected https://cad.onshape.com/documents/<d>/w/<w>/e/<e>")
        return 2
    did, wvm, wvmid, eid = m.groups()
    part_id = sys.argv[2] if len(sys.argv) > 2 else None
    config = sys.argv[3] if len(sys.argv) > 3 else ""

    try:
        auth = (os.environ["ONSHAPE_ACCESS_KEY"], os.environ["ONSHAPE_SECRET_KEY"])
    except KeyError:
        print("Set ONSHAPE_ACCESS_KEY and ONSHAPE_SECRET_KEY in the environment.")
        return 3

    with httpx.Client(base_url=BASE, auth=auth, follow_redirects=True, timeout=60) as client:
        print("Document:")
        r = client.get(f"/api/documents/{did}", headers={"Accept": "application/json"})
        log_response(r)
        if r.is_success:
            print(f"  name = {r.json().get('name')!r}")

        print("Parts:")
        r = client.get(
            f"/api/parts/d/{did}/{wvm}/{wvmid}/e/{eid}", headers={"Accept": "application/json"}
        )
        log_response(r)
        r.raise_for_status()
        parts = r.json()
        for p in parts:
            print(f"  partId={p.get('partId')!r:10} name={p.get('name')!r}")
        if part_id is None:
            part_id = parts[0]["partId"] if parts else None
            print(f"No partId given; using the first part: {part_id!r}")
        if part_id is None:
            return 1

        print("STL export:")
        # httpx drops auth on cross-host redirects (cad -> cad-usw2), so follow by hand.
        r = client.get(
            f"/api/partstudios/d/{did}/{wvm}/{wvmid}/e/{eid}/stl",
            params={
                "partIds": part_id,
                "mode": "binary",
                "units": "millimeter",
                "grouping": "true",
                "configuration": config,
            },
            headers={"Accept": "application/vnd.onshape.v1+octet-stream"},
            follow_redirects=False,
        )
        log_response(r)
        if r.status_code in (302, 303, 307, 308):
            loc = httpx.URL(r.headers["location"])
            if loc.scheme != "https" or not ONSHAPE_HOST_RE.match(loc.host):
                print(f"Refusing redirect to non-Onshape host {loc.host!r}")
                return 4
            r = client.get(loc, headers={"Accept": "application/octet-stream"})
            log_response(r)
        r.raise_for_status()
        suffix = "_cfg" if config else ""
        out = Path(__file__).resolve().parent / f"spike_{part_id}{suffix}.stl"
        out.write_bytes(r.content)
        # Binary STL: 80-byte header, uint32 triangle count.
        tris = int.from_bytes(r.content[80:84], "little") if len(r.content) >= 84 else -1
        print(f"Saved {out} ({tris} triangles)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
