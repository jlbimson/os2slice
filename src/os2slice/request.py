"""Turn untrusted input into a validated ExportRequest.

This is the security boundary: every request that reaches the listener may
come from a hostile page. See docs/ARCHITECTURE.md → Validation rules.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Collection
from dataclasses import dataclass
from typing import Literal, cast
from urllib.parse import parse_qsl, urlsplit

from os2slice.errors import BadRequest

Wvm = Literal["w", "v", "m"]
Fmt = Literal["stl", "3mf", "step"]

HEX24_RE = re.compile(r"[0-9a-f]{24}")
PART_ID_RE = re.compile(r"[A-Za-z0-9_+\-]{1,32}")
SLICER_KEY_RE = re.compile(r"[a-z0-9_\-]{1,32}")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
ONSHAPE_HOST_RE = re.compile(r"[a-z0-9-]+\.onshape\.com")
ONSHAPE_URL_PATH_RE = re.compile(
    r"/documents/([0-9a-f]{24})/([wvm])/([0-9a-f]{24})/e/([0-9a-f]{24})/?"
)

FORMATS: tuple[Fmt, ...] = ("stl", "3mf", "step")
MAX_CONFIG_LEN = 2048
MAX_FIELDS = 32
CONFIG_PLACEHOLDER = "{$configuration}"
PRINT_TARGET = "bambuddy"  # ExportRequest.slicer for the print path

REQUIRED_PARAMS = frozenset({"slicer", "d", "wv", "wvid", "e"})
OPTIONAL_PARAMS = frozenset({"p", "c", "fmt"})
# Onshape appends these to every extension Action URL. Allowed, never used.
ONSHAPE_EXTRA_PARAMS = frozenset(
    {"companyId", "sessionCompanyId", "server", "userId", "clientId", "locale", "theme"}
)
KNOWN_PARAMS = REQUIRED_PARAMS | OPTIONAL_PARAMS | ONSHAPE_EXTRA_PARAMS


@dataclass(frozen=True)
class ExportRequest:
    slicer: str
    document_id: str
    wvm: Wvm
    wvm_id: str
    element_id: str
    part_id: str | None
    configuration: str = ""
    fmt: Fmt = "stl"


def parse_query(query: str, slicers: Collection[str], default_fmt: Fmt = "stl") -> ExportRequest:
    """Validate a raw (still percent-encoded) query string from the listener."""
    try:
        pairs = parse_qsl(
            query, keep_blank_values=True, strict_parsing=True, max_num_fields=MAX_FIELDS
        )
    except ValueError as e:
        raise BadRequest("Malformed query string") from e

    params: dict[str, str] = {}
    for key, value in pairs:
        if key not in KNOWN_PARAMS:
            raise BadRequest(f"Unknown parameter {_show(key)!s}")
        if key in params:
            raise BadRequest(f"Parameter {key!r} given more than once")
        params[key] = value
    return _from_params(params, slicers, default_fmt)


def _from_params(
    params: dict[str, str], slicers: Collection[str], default_fmt: Fmt
) -> ExportRequest:
    missing = sorted(REQUIRED_PARAMS - params.keys())
    if missing:
        raise BadRequest(f"Missing parameter(s): {', '.join(missing)}")

    slicer = params["slicer"]
    _check_placeholder("slicer", slicer)
    if not SLICER_KEY_RE.fullmatch(slicer):
        raise BadRequest(f"Invalid slicer name {_show(slicer)}")
    if slicer not in slicers:
        raise BadRequest(*_unknown_slicer(slicer, slicers))

    ids = {}
    for key in ("d", "wvid", "e"):
        value = params[key]
        _check_placeholder(key, value)
        if not HEX24_RE.fullmatch(value):
            raise BadRequest(f"Invalid {key}: expected a 24-character Onshape ID")
        ids[key] = value

    wvm = params["wv"]
    _check_placeholder("wv", wvm)
    if wvm not in ("w", "v", "m"):
        raise BadRequest("Invalid wv: expected w, v or m")

    part_id = params.get("p") or None
    if part_id is not None:
        _check_placeholder("p", part_id)
        if not PART_ID_RE.fullmatch(part_id):
            raise BadRequest("Invalid part ID")

    configuration = normalize_configuration(params.get("c", ""))

    fmt = params.get("fmt") or default_fmt
    if fmt not in FORMATS:
        raise BadRequest(f"Invalid fmt: expected one of {', '.join(FORMATS)}")

    return ExportRequest(
        slicer=slicer,
        document_id=ids["d"],
        wvm=cast(Wvm, wvm),
        wvm_id=ids["wvid"],
        element_id=ids["e"],
        part_id=part_id,
        configuration=configuration,
        fmt=cast(Fmt, fmt),
    )


def parse_print_query(query: str, default_fmt: Fmt = "stl") -> ExportRequest:
    """Validate the query of a BamBuddy print link (/print?d=…&wv=…&wvid=…&e=…&p=…&c=…).

    Same rules as parse_query, but there is no `slicer` parameter.
    """
    try:
        pairs = parse_qsl(
            query, keep_blank_values=True, strict_parsing=True, max_num_fields=MAX_FIELDS
        )
    except ValueError as e:
        raise BadRequest("Malformed query string") from e
    params: dict[str, str] = {}
    for key, value in pairs:
        if key not in KNOWN_PARAMS or key == "slicer":
            raise BadRequest(f"Unknown parameter {_show(key)!s}")
        if key in params:
            raise BadRequest(f"Parameter {key!r} given more than once")
        params[key] = value
    params["slicer"] = PRINT_TARGET
    return _from_params(params, (PRINT_TARGET,), default_fmt)


def normalize_configuration(value: str) -> str:
    """Validate a decoded configuration string. The unresolved placeholder means default."""
    if value == CONFIG_PLACEHOLDER:
        return ""
    if len(value) > MAX_CONFIG_LEN:
        raise BadRequest(f"Configuration string longer than {MAX_CONFIG_LEN} characters")
    if CONTROL_RE.search(value):
        raise BadRequest("Configuration string contains control characters")
    _check_placeholder("c", value)
    return value


def parse_listener_url(
    url: str, slicers: Collection[str], default_fmt: Fmt = "stl"
) -> ExportRequest:
    """Parse a full listener URL, as given to `os2slice handle`.

    Accepts http://localhost[:port]/open?… and http://127.0.0.1[:port]/open?…,
    plus os2slice://open?… for the custom-scheme fallback (D-3, option A).
    """
    parts = urlsplit(url.strip())
    if parts.scheme == "os2slice":
        action = parts.netloc or parts.path.strip("/")
        if action != "open" or (parts.netloc and parts.path not in ("", "/")):
            raise BadRequest("Expected an os2slice://open?… URL")
    elif parts.scheme == "http":
        if parts.hostname not in ("localhost", "127.0.0.1") or parts.path != "/open":
            raise BadRequest("Expected an http://localhost:<port>/open?… URL")
    else:
        raise BadRequest("Expected an http://localhost:<port>/open?… or os2slice://open?… URL")
    if parts.fragment:
        raise BadRequest("Unexpected #fragment in URL")
    return parse_query(parts.query, slicers, default_fmt)


def parse_onshape_url(
    url: str,
    slicer: str,
    slicers: Collection[str],
    part_id: str | None,
    configuration: str = "",
    default_fmt: Fmt = "stl",
) -> ExportRequest:
    """Build a request from a normal Onshape browser URL (for `os2slice send`).

    https://cad.onshape.com/documents/<d>/<w|v|m>/<id>/e/<e>[?configuration=…]
    A configuration in the URL is used unless one is passed explicitly.
    """
    parts = urlsplit(url.strip())
    if parts.scheme != "https" or not ONSHAPE_HOST_RE.fullmatch(parts.hostname or ""):
        raise BadRequest("Expected an https://cad.onshape.com/documents/… URL")
    m = ONSHAPE_URL_PATH_RE.fullmatch(parts.path)
    if not m:
        raise BadRequest(
            "Expected a Part Studio URL like https://cad.onshape.com/documents/<d>/w/<w>/e/<e>"
        )
    if not configuration:
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        configuration = query.get("configuration", "")
    did, wvm, wvm_id, eid = m.groups()
    params = {"slicer": slicer, "d": did, "wv": wvm, "wvid": wvm_id, "e": eid, "c": configuration}
    if part_id:
        params["p"] = part_id
    return _from_params(params, slicers, default_fmt)


def _check_placeholder(key: str, value: str) -> None:
    if "{$" in value:
        raise BadRequest(
            f"Parameter {key!r} holds an unresolved Onshape placeholder",
            "Check the extension's Location/Context: it must be a Part context menu",
        )


def _unknown_slicer(slicer: str, slicers: Collection[str]) -> tuple[str, str]:
    configured = ", ".join(sorted(slicers)) or "none"
    if slicer == "preform" and sys.platform.startswith("linux"):
        return (
            "PreForm isn't supported on Linux",
            "Use it from Windows or macOS (docs/DECISIONS.md D-2)",
        )
    return (
        f"Slicer {slicer!r} isn't configured",
        f"Add [slicers.{slicer}] to config.toml (configured: {configured})",
    )


def _show(value: str, limit: int = 40) -> str:
    """repr() of attacker-controlled text, shortened, for error messages."""
    return repr(value if len(value) <= limit else value[:limit] + "…")
