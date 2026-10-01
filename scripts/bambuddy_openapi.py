# /// script
# requires-python = ">=3.10"
# dependencies = ["httpx"]
# ///
"""Phase 2 spike: summarize a BamBuddy instance's OpenAPI schema. Read-only.

    export BAMBUDDY_URL=http://<nuc>:8000          # not secret
    export BAMBUDDY_API_KEY=...                     # optional; from a 0600 env file, never argv
    uv run scripts/bambuddy_openapi.py [keyword ...]

Saves the full schema to scripts/bambuddy_openapi.json (gitignored) and prints
every operation whose path, tag or summary matches a keyword (default: the
ones the print pipeline needs), with its parameters and request body schema.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

DEFAULT_KEYWORDS = [
    "printer",
    "status",
    "library",
    "file",
    "upload",
    "slice",
    "preset",
    "profile",
    "queue",
    "print",
    "permission",
    "api-key",
    "system/info",
]
OUT = Path(__file__).resolve().parent / "bambuddy_openapi.json"


def ref_name(schema: dict[str, Any]) -> str:
    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[-1]
    if schema.get("type") == "array":
        return f"list[{ref_name(schema.get('items', {}))}]"
    return schema.get("type", "?")


def show_schema(spec: dict[str, Any], name: str, indent: str = "      ") -> None:
    schema = spec.get("components", {}).get("schemas", {}).get(name)
    if not schema:
        return
    required = set(schema.get("required", []))
    for prop, s in schema.get("properties", {}).items():
        flag = "*" if prop in required else " "
        extra = f" enum={s['enum']}" if "enum" in s else ""
        print(f"{indent}{flag}{prop}: {ref_name(s)}{extra}")


def main() -> int:
    base = os.environ.get("BAMBUDDY_URL", "").rstrip("/")
    if not base.startswith(("http://", "https://")):
        print("Set BAMBUDDY_URL, e.g. http://nuc:8000")
        return 2
    headers = {"Accept": "application/json"}
    if key := os.environ.get("BAMBUDDY_API_KEY"):
        headers["X-API-Key"] = key

    with httpx.Client(base_url=base, headers=headers, timeout=30) as client:
        for path in ("/openapi.json", "/api/v1/openapi.json"):
            r = client.get(path)
            print(f"GET {path} -> {r.status_code}")
            if r.status_code == 200:
                break
        else:
            return 1
        spec = r.json()
        OUT.write_text(json.dumps(spec, indent=2))
        print(f"saved {OUT}")
        r = client.get("/api/v1/system/info")
        print(f"GET /api/v1/system/info -> {r.status_code}")
        if r.status_code == 200:
            info = r.json()
            print("  " + json.dumps({k: info[k] for k in list(info)[:8]}))

    info = spec.get("info", {})
    print(f"\n{info.get('title')} {info.get('version')}: {len(spec.get('paths', {}))} paths")
    keywords = [k.lower() for k in (sys.argv[1:] or DEFAULT_KEYWORDS)]
    for path, ops in sorted(spec.get("paths", {}).items()):
        for method, op in ops.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            text = " ".join([path, op.get("summary", ""), *op.get("tags", [])]).lower()
            if not any(k in text for k in keywords):
                continue
            print(f"\n{method.upper()} {path}  — {op.get('summary', '')}")
            for p in op.get("parameters", []):
                req = "*" if p.get("required") else " "
                print(f"    {req}{p['in']}:{p['name']} ({ref_name(p.get('schema', {}))})")
            for ctype, body in op.get("requestBody", {}).get("content", {}).items():
                name = ref_name(body.get("schema", {}))
                print(f"    body {ctype}: {name}")
                show_schema(spec, name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
