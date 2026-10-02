"""A small TOML writer for os2slice's own config schema (D-28), and atomic file writes.

The config page saves `config.toml` through this module; there is no TOML writer in the
standard library and D-28 rules out a new dependency. It covers what our schema uses,
not all of TOML:

- values: `str`, `bool`, `int` (64-bit), `float` (incl. inf/nan), lists (tuples become
  lists) of any of these, and dicts;
- a dict is written as a `[a.b]` table, or as an inline table `{ k = v, ... }` when the
  caller asks for it with `inline_keys` (a set of key names, or a predicate over the key
  path), e.g. `inline_keys={"profiles"}` for `profiles = { printer = "...", ... }`
  (docs/MODULES.md); dicts inside lists are always inline;
- no arrays of tables (`[[x]]`), no dates, no comments other than a header
  (`comment_header`). `None` and any other type raise `TypeError` naming the key path.

Output is deterministic: keys in insertion order, and within each table its plain
values come before its sub-tables (TOML requires that). Strings are always basic
strings: `"` and `\\` are escaped, as are control characters (`\\n`, `\\t`, ... or
`\\uXXXX`); other unicode is written as is (TOML files are UTF-8). Keys are bare when
they match `[A-Za-z0-9_-]+`, quoted otherwise (`[targets.farm.models."X1 Carbon"]`).
The invariant, tested: `tomllib.loads(dumps(d)) == d` for every supported `d` (NaN
aside, which never equals itself).

Comments in an existing file are not preserved: the page writes the whole file anew,
with a `comment_header` saying it was written by os2slice.

`write_atomic` writes a temporary file in the target's directory (0600 by default),
fsyncs it, and renames it over the target, so readers see the old file or the new one,
never half of one.
"""

from __future__ import annotations

import contextlib
import errno
import math
import os
import re
import tempfile
from collections.abc import Callable, Collection, Mapping
from pathlib import Path
from typing import Any

KeyPath = tuple[str, ...]
InlineSpec = Collection[str] | Callable[[KeyPath], bool] | None

_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}
_INT_MIN, _INT_MAX = -(2**63), 2**63 - 1


def _where(path: KeyPath) -> str:
    return ".".join(path) if path else "<root>"


def _quote(s: str, path: KeyPath) -> str:
    out = []
    for ch in s:
        code = ord(ch)
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif code < 0x20 or code == 0x7F:
            out.append(f"\\u{code:04X}")
        elif 0xD800 <= code <= 0xDFFF:
            raise ValueError(f"{_where(path)}: string has a lone surrogate, not valid in TOML")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _key(k: object, path: KeyPath) -> str:
    if not isinstance(k, str):
        raise TypeError(f"{_where(path)}: key {k!r} is a {type(k).__name__}, not a str")
    return k if _BARE_KEY.fullmatch(k) else _quote(k, (*path, k))


def _float(f: float) -> str:
    if math.isnan(f):
        return "nan"
    if math.isinf(f):
        return "inf" if f > 0 else "-inf"
    r = repr(f)  # shortest round-tripping form: "1.0", "1e-05", "1.5e+16"
    return r if ("." in r or "e" in r) else r + ".0"


def _is_table(v: Any) -> bool:
    return isinstance(v, Mapping)


def _value(v: Any, path: KeyPath) -> str:
    # bool first: it is a subclass of int.
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        if not _INT_MIN <= v <= _INT_MAX:
            raise ValueError(f"{_where(path)}: integer {v} doesn't fit in 64 bits")
        return str(v)
    if isinstance(v, float):
        return _float(v)
    if isinstance(v, str):
        return _quote(v, path)
    if isinstance(v, (list, tuple)):
        items = [_value(x, (*path, f"[{i}]")) for i, x in enumerate(v)]
        return "[" + ", ".join(items) + "]"
    if _is_table(v):
        if not v:
            return "{}"
        parts = [f"{_key(k, path)} = {_value(x, (*path, k))}" for k, x in v.items()]
        return "{ " + ", ".join(parts) + " }"
    raise TypeError(f"{_where(path)}: can't write a {type(v).__name__} to TOML")


def _inline_pred(inline_keys: InlineSpec) -> Callable[[KeyPath], bool]:
    if inline_keys is None:
        return lambda _p: False
    if callable(inline_keys):
        return inline_keys
    names = frozenset(inline_keys)
    return lambda p: p[-1] in names


def _emit(
    table: Mapping[str, Any],
    path: KeyPath,
    header: str,
    inline: Callable[[KeyPath], bool],
    out: list[str],
) -> None:
    plain: list[str] = []
    subs: list[tuple[str, str, Mapping[str, Any]]] = []
    for k, v in table.items():
        kp = (*path, k)
        key = _key(k, path)
        if _is_table(v) and not inline(kp):
            subs.append((k, key, v))
        else:
            plain.append(f"{key} = {_value(v, kp)}")
    # A table needs its own header when it has values, or is empty (so it still
    # exists after reading back). With only sub-tables, their headers define it.
    if path and (plain or not subs):
        if out:
            out.append("")
        out.append(f"[{header}]")
    out.extend(plain)
    for k, key, sub in subs:
        _emit(sub, (*path, k), f"{header}.{key}" if header else key, inline, out)


def dumps(data: Mapping[str, Any], *, inline_keys: InlineSpec = None) -> str:
    """`data` as TOML text. `inline_keys`: names (or a predicate over the key path) of
    dict values to write as inline tables instead of `[section]` tables."""
    if not _is_table(data):
        raise TypeError(f"<root>: TOML document must be a mapping, not {type(data).__name__}")
    out: list[str] = []
    _emit(data, (), "", _inline_pred(inline_keys), out)
    return "\n".join(out) + "\n" if out else ""


def comment_header(text: str) -> str:
    """`text` as `# ` comment lines (every line, so nothing in it can become TOML)."""
    lines = text.splitlines() or [""]
    return "".join(f"# {ln}".rstrip() + "\n" for ln in lines)


def write_atomic(path: Path, text: str, mode: int = 0o600) -> None:
    """Replace `path` with `text` (UTF-8) atomically, the new file having `mode`.

    The temp file lives in the same directory (so `os.replace` is a rename on one
    filesystem) and is removed if anything fails before the rename.

    A file that is itself a mount point (Docker's single-file bind mount of config.toml)
    can't be renamed over (EBUSY). It is then rewritten in place: not atomic, but the
    only way to change it from inside the container. It keeps its own mode.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            os.fchmod(f.fileno(), mode)
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.replace(tmp, path)
        except OSError as e:
            if e.errno != errno.EBUSY:
                raise
            _write_in_place(path, text)
            os.unlink(tmp)
            return
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    # Make the rename itself durable (best effort; not every OS/filesystem allows it).
    with contextlib.suppress(OSError):
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)


def _write_in_place(path: Path, text: str) -> None:
    with open(path, "r+", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.truncate()
        f.flush()
        os.fsync(f.fileno())
