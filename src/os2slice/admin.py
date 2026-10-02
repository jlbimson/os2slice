"""The config page, `/admin` (D-28): server-rendered forms that edit config.toml.

Rules (docs/ARCHITECTURE.md, the `/admin` rows), in the order they are checked:

1. The global Host (and identity) check, in `server._Handler._dispatch`, as everywhere.
2. No admin password set → 503 on every `/admin` path. It is set only on the server
   (`os2slice admin-password`); a browser can't claim a fresh install.
3. Every POST: `Sec-Fetch-Site: same-origin` (or, from a browser that sends no
   Sec-Fetch-Site, an `Origin` equal to the Host), and an `Origin`, when sent, equal to
   the Host. Else 403.
4. `GET`/`POST /admin/login` need no session. The POST redeems a single-use CSRF token,
   then the rate limit (per peer address, `AdminStore`; 429 while locked out), then a
   constant-time password check (401 when wrong). Success sets `os2slice_admin`
   (`Path=/admin; HttpOnly; SameSite=Strict`, `Secure` under TLS or identity "lan").
5. Every other route needs that session, else 303 → `/admin/login`.
6. Every other POST: the body (urlencoded, ≤ 64 KB, ≤ 400 fields, field names checked
   per form), then a single-use CSRF token bound to the session and the form's action,
   before anything else happens.

GETs only read: they may call a module's `check()`, `profiles()` or `printers()`. A
save reads the raw TOML, applies the edit, validates it with `config.parse` (the same
rules as at start), builds the modules once as a trial (so a missing secret fails
before writing), stores any new secrets, writes the file atomically and calls
`Service.reload()`. Secrets are write-only: pages say "set" or "not set", never echo
a value, and they never go into config.toml.
"""

from __future__ import annotations

import hashlib
import html
import logging
import math
import re
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import SplitResult, parse_qsl, urlencode, urlsplit

from os2slice import __version__, auth, config, extra_settings, tomlwrite
from os2slice.adminauth import SESSION_TTL, PasswordError
from os2slice.errors import BadRequest, ConfigError, Os2sliceError
from os2slice.logsetup import log_path
from os2slice.modules import registry
from os2slice.modules.base import Field, ModuleSpec, ProfileCatalog
from os2slice.request import SLICER_KEY_RE
from os2slice.runtime import DOCKER, HOME_ASSISTANT, runtime
from os2slice.settings import (
    BED_TYPES,
    COPIES_RANGE,
    INFILL_RANGE,
    PLATE_LABELS,
    SHELL_RANGE,
    SUPPORTS,
    WALLS_RANGE,
    PrintSettings,
)

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

if TYPE_CHECKING:
    from os2slice.server import Service

log = logging.getLogger(__name__)

COOKIE = "os2slice_admin"
LOGIN = "/admin/login"
CSRF_USER = "admin"
MAX_BODY = 65_536
MAX_FIELDS = 400
MAX_MODEL_ROWS = 50
FIELD_NAME_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
MODEL_ROW_RE = re.compile(r"m([0-9]{1,2})\.(model|slicer|printer|process|filament|bed_type)")
TEXT_RE = re.compile(r"[^\x00-\x1f\x7f]{1,200}")  # a one-line value: model names, profiles
LOG_LINES = 200
JOB_ROWS = 50
SECTIONS = (
    ("/admin", "Overview"),
    ("/admin/slicers", "Slicers"),
    ("/admin/targets", "Targets"),
    ("/admin/printers", "Printers"),
    ("/admin/onshape", "Onshape"),
    ("/admin/server", "Server"),
    ("/admin/defaults", "Print defaults"),
    ("/admin/panel", "Onshape panel"),
    ("/admin/jobs", "Jobs"),
    ("/admin/log", "Log"),
    ("/admin/secrets", "Secrets"),
    ("/admin/password", "Password"),
)
CSS = """<style>
body { max-width:60rem; }
nav.admin { display:flex; flex-wrap:wrap; gap:.2rem .8rem; margin:0 0 .6rem; }
nav.admin a.here { font-weight:600; text-decoration:none; color:var(--fg); }
table { border-collapse:collapse; width:100%; margin:.6rem 0; font-size:.92rem; }
th,td { text-align:left; vertical-align:top; border-bottom:1px solid var(--line);
  padding:.3rem .4rem; overflow-wrap:anywhere; }
.scroll { overflow-x:auto; }
input[type=text],input[type=password],textarea { display:block; width:100%;
  box-sizing:border-box; margin-top:.2rem; padding:.4rem; background:var(--bg);
  color:var(--fg); border:1px solid var(--line); border-radius:6px; font:inherit; }
td input[type=text], td select { margin:0; min-width:7rem; }
button.small { width:auto; margin:0; padding:.25rem .7rem; font-size:.9rem; }
.buttons { display:flex; gap:.6rem; } .buttons button { flex:1; }
button.second { background:var(--line); color:var(--fg); }
.msg { border:1px solid var(--line); border-left:4px solid var(--accent); padding:.5rem .7rem;
  margin:.6rem 0; border-radius:4px; }
.msg.bad { border-left-color:#c62828; } .msg.good { border-left-color:#2e7d32; }
.ok { color:#2e7d32; } .bad { color:#c62828; } .warn { color:#b26a00; }
form.inline { display:inline; } .file { color:var(--muted); font-size:.9rem; }
pre.log { font-size:.8rem; overflow-x:auto; white-space:pre; border:1px solid var(--line);
  padding:.5rem; border-radius:6px; }
</style>"""


class Refused(Os2sliceError):
    exit_code = 2
    http_status = 403


class Invalid(Exception):
    """A form value the config rules refuse; the form is shown again with the message."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _e(text: object) -> str:
    return html.escape(str(text), quote=True)


# ---------------------------------------------------------------------------
# Request plumbing


@dataclass
class Ctx:
    """One admin request: the handler and the admin session token ("" on the login page)."""

    h: Any  # server._Handler
    token: str

    @property
    def svc(self) -> Service:
        return self.h.svc  # type: ignore[no-any-return]

    def _bind(self, action: str) -> str:
        sid = hashlib.sha256(self.token.encode()).hexdigest()[:32] if self.token else ""
        return f"{action}|{sid}"

    def csrf(self, action: str) -> str:
        token = self.svc.tokens.issue(CSRF_USER, self._bind(action))
        return f'<input type="hidden" name="csrf" value="{_e(token)}">'

    def redeem(self, action: str, token: str) -> bool:
        return self.svc.tokens.redeem(token, CSRF_USER, self._bind(action))

    def form(self, action: str, inner: str, button: str = "Save", cls: str = "") -> str:
        bcls = f' class="{cls}"' if cls else ""
        fcls = ' class="inline"' if cls else ""
        return (
            f'<form method="post" action="{_e(action)}"{fcls}>{self.csrf(action)}{inner}'
            f'<button type="submit"{bcls}>{_e(button)}</button></form>'
        )

    def page(self, title: str, body: str, status: int = 200, here: str = "") -> None:
        self.h._page(status, title, _chrome(self, here) + body)

    def redirect(self, location: str) -> None:
        _redirect(self.h, location)


def _redirect(h: Any, location: str) -> None:
    h.send_response(303)
    h.send_header("Location", location)
    h._headers(0)


def _send_page(
    h: Any, status: int, title: str, body: str, headers: Mapping[str, str] | None = None
) -> None:
    from os2slice.server import _layout

    doc = _layout(title, CSS + body, False).encode()
    h.send_response(status)
    h.send_header("Content-Type", "text/html; charset=utf-8")
    for k, v in (headers or {}).items():
        h.send_header(k, v)
    h._headers(len(doc))
    h.wfile.write(doc)


def _require_same_origin(h: Any) -> None:
    site = h.headers.get("Sec-Fetch-Site")
    origin = h.headers.get("Origin")
    host = h.headers.get("Host", "")
    if site is None:
        # A browser without Fetch Metadata: only a same-origin Origin will do.
        if origin is None or urlsplit(origin).netloc != host:
            raise Refused("Cross-site form submission refused")
    elif site != "same-origin":
        raise Refused("Cross-site form submission refused")
    if origin is not None and urlsplit(origin).netloc != host:
        raise Refused("Form came from another site")


def _read_form(h: Any) -> dict[str, str]:
    ctype = h.headers.get("Content-Type", "").split(";")[0].strip()
    if ctype != "application/x-www-form-urlencoded":
        raise BadRequest("Expected a form submission")
    try:
        length = int(h.headers.get("Content-Length", ""))
    except ValueError as e:
        raise BadRequest("Missing Content-Length") from e
    if not 0 < length <= MAX_BODY:
        raise BadRequest("Form too large")
    raw = h.rfile.read(length).decode("utf-8", errors="strict")
    try:
        pairs = parse_qsl(
            raw, keep_blank_values=True, strict_parsing=True, max_num_fields=MAX_FIELDS
        )
    except ValueError as e:
        raise BadRequest("Malformed form") from e
    form: dict[str, str] = {}
    for k, v in pairs:
        if not FIELD_NAME_RE.fullmatch(k) or k in form:
            raise BadRequest(f"Unexpected form field {k[:30]!r}")
        form[k] = v
    return form


def _expect(
    form: Mapping[str, str], allowed: Iterable[str], pattern: re.Pattern[str] | None = None
) -> None:
    names = set(allowed) | {"csrf"}
    for k in form:
        if k not in names and not (pattern and pattern.fullmatch(k)):
            raise BadRequest(f"Unexpected form field {k[:30]!r}")


def _query(query: str, allowed: Iterable[str]) -> dict[str, str]:
    try:
        pairs = parse_qsl(query, keep_blank_values=True, max_num_fields=8)
    except ValueError as e:
        raise BadRequest("Malformed query") from e
    names = set(allowed)
    out: dict[str, str] = {}
    for k, v in pairs:
        if k not in names or k in out or len(v) > 200:
            raise BadRequest(f"Unexpected parameter {k[:30]!r}")
        out[k] = v
    return out


def _secure(svc: Service) -> bool:
    s = svc.cfg.server
    return s.identity == "lan" or s.tls_cert is not None


def _set_session_cookie(h: Any, value: str, max_age: int) -> None:
    secure = "; Secure" if _secure(h.svc) else ""
    h._cookies.append(
        f"{COOKIE}={value}; Path=/admin; Max-Age={max_age}{secure}; HttpOnly; SameSite=Strict"
    )


Get = Callable[[Ctx, dict[str, str]], None]
Post = Callable[[Ctx, dict[str, str]], None]


def handle(h: Any, method: str, url: SplitResult) -> None:
    """Every `/admin` and `/admin/...` request, after the global Host check."""
    path = url.path.rstrip("/") or "/"
    if path not in GET_ROUTES and path not in POST_ROUTES and path != LOGIN:
        h._page(404, "Not found", "<p>Nothing here.</p>")
        return
    svc: Service = h.svc
    if not svc.admin.has_password():
        _send_page(h, 503, "Config page is off", _OFF_PAGE)
        return
    if method == "POST" and (path in POST_ROUTES or path == LOGIN):
        _require_same_origin(h)
    elif not (method == "GET" and (path in GET_ROUTES or path == LOGIN)):
        h._method_not_allowed()
        return
    token = h._cookie(COOKIE)
    signed_in = bool(token) and svc.admin.check_session(token)
    if path == LOGIN:
        if method == "GET":
            _query(url.query, ())
            if signed_in:
                _redirect(h, "/admin")
            else:
                _login_page(Ctx(h, ""))
        else:
            _post_login(Ctx(h, ""))
        return
    if not signed_in:
        _redirect(h, LOGIN)
        return
    ctx = Ctx(h, token)
    if method == "GET":
        fn, params = GET_ROUTES[path]
        fn(ctx, _query(url.query, params))
        return
    form = _read_form(h)
    if not ctx.redeem(path, form.pop("csrf", "")):
        raise Refused("This form has expired or was already used", "Reload the page and try again")
    POST_ROUTES[path](ctx, form)


_OFF_PAGE = (
    "<p><b>No admin password is set, so the config page is off.</b></p>"
    "<p>Set one on the server: run <code>os2slice admin-password</code> there "
    "(it can't be set from a browser).</p>"
)


# ---------------------------------------------------------------------------
# Page chrome


def _chrome(ctx: Ctx, here: str) -> str:
    links = " ".join(
        f'<a href="{p}"{" class=here" if p == here else ""}>{_e(label)}</a>'
        for p, label in SECTIONS
    )
    logout = ctx.form("/admin/logout", "", "Sign out", cls="small")
    notes = "".join(f'<div class="msg">{_e(n)}</div>' for n in _restart_notes(ctx.svc))
    where = runtime()
    if where == HOME_ASSISTANT:
        notes += (
            '<div class="msg">Running as the Home Assistant add-on: changes made here '
            "persist across restarts until the options on the add-on's Configuration tab "
            "change (or reset_config is on); then config.toml is written again from them. "
            "Secrets saved here are used while the matching option on that tab is empty; "
            "a filled-in option replaces them at the next start.</div>"
        )
    elif where == DOCKER:
        notes += (
            '<div class="msg">Running in Docker: changes made here are written to '
            "docker/config.toml on the host (comments aren't kept). Secrets saved here go to "
            "the os2slice-data volume and win over the same ones in .env. After editing "
            "config.toml on the host, run docker compose restart os2slice.</div>"
        )
    return (
        f'{CSS}<nav class="admin">{links} {logout}</nav>'
        f'<p class="file">Editing {_e(ctx.svc.cfg.path)}</p>{notes}'
    )


def _restart_notes(svc: Service) -> list[str]:
    run, f = svc.cfg, svc.file_cfg
    notes = []
    if f.server != run.server:
        notes.append(
            "Server settings (bind, port, hosts, identity, TLS) changed in the file: restart "
            "the service to apply them."
        )
    onshape = ("onshape_base_url", "onshape_auth", "oauth_client_id", "oauth_base_url")
    if any(getattr(f, k) != getattr(run, k) for k in onshape):
        notes.append("Onshape settings changed in the file: restart the service to apply them.")
    notes.extend(sorted(svc.restart_notes))
    return notes


def _msg(text: str, kind: str = "") -> str:
    return f'<div class="msg {kind}">{_e(text)}</div>' if text else ""


def _saved(params: Mapping[str, str]) -> str:
    return _msg("Saved; the service reloaded its config.", "good") if "saved" in params else ""


# ---------------------------------------------------------------------------
# Form widgets


def _text(
    name: str, label: str, value: str = "", *, help: str = "", required: bool = False,
    kind: str = "text", attrs: str = "",
) -> str:  # fmt: skip
    req = ' <span class="muted">(required)</span>' if required else ""
    hint = f'<span class="muted">{_e(help)}</span>' if help else ""
    return (
        f'<label>{_e(label)}{req}<input type="{kind}" name="{_e(name)}" '
        f'value="{_e(value)}"{attrs}></label>{hint}'
    )


def _secret_input(name: str, label: str, is_set: bool, help: str = "") -> str:
    state = "set" if is_set else "not set"
    hint = "leave empty to keep it" if is_set else "write-only"
    return (
        f'<label>{_e(label)} <span class="muted">({state})</span><input type="password" '
        f'name="{_e(name)}" value="" autocomplete="new-password" placeholder="{hint}"></label>'
        + (f'<span class="muted">{_e(help)}</span>' if help else "")
    )


def _check(name: str, label: str, on: bool, help: str = "") -> str:
    checked = " checked" if on else ""
    hint = f' <span class="muted">{_e(help)}</span>' if help else ""
    return (
        f'<label class="check"><input type="checkbox" name="{_e(name)}" value="on"{checked}>'
        f"{_e(label)}</label>{hint}"
    )


def _opts(choices: Iterable[tuple[str, str]], selected: str) -> str:
    return "".join(
        f'<option value="{_e(v)}"{" selected" if v == selected else ""}>{_e(label)}</option>'
        for v, label in choices
    )


def _select(
    name: str, label: str, choices: Iterable[tuple[str, str]], selected: str, help: str = ""
) -> str:
    choices = list(choices)
    if selected and selected not in {v for v, _ in choices}:
        choices.append((selected, f"{selected} (not known)"))
    hint = f'<span class="muted">{_e(help)}</span>' if help else ""
    return (
        f'<label>{_e(label)}<select name="{_e(name)}">{_opts(choices, selected)}</select>'
        f"</label>{hint}"
    )


def _area(name: str, label: str, value: str, help: str = "", required: bool = False) -> str:
    req = ' <span class="muted">(required)</span>' if required else ""
    hint = f'<span class="muted">{_e(help)}</span>' if help else ""
    return (
        f'<label>{_e(label)}{req}<textarea name="{_e(name)}" rows="4">{_e(value)}</textarea>'
        f"</label>{hint}"
    )


def _lines(value: str) -> list[str]:
    return [ln.strip() for ln in value.splitlines() if ln.strip()]


def _plate_choices() -> list[tuple[str, str]]:
    return [("", "(the profile's default)")] + [(b, PLATE_LABELS.get(b, b)) for b in BED_TYPES]


def _datalist(ident: str, names: Iterable[str]) -> str:
    opts = "".join(f'<option value="{_e(n)}">' for n in list(names)[:2000])
    return f'<datalist id="{_e(ident)}">{opts}</datalist>' if opts else ""


# ---------------------------------------------------------------------------
# Reading and saving the config

_SAVE_LOCK = threading.Lock()


def _raw(svc: Service) -> dict[str, Any]:
    path = svc.cfg.path
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"Can't read {path}: {e}", f"Fix {path} by hand") from e


def _table(data: dict[str, Any], name: str) -> dict[str, Any]:
    tbl = data.setdefault(name, {})
    if not isinstance(tbl, dict):
        raise Invalid(f"[{name}] in the file isn't a table; fix it by hand")
    return tbl


def _parse(data: dict[str, Any], svc: Service) -> config.Config:
    try:
        return config.parse(data, svc.cfg.path)
    except ConfigError as e:
        raise Invalid(e.message.removeprefix("config.toml: ")) from e


def render_toml(data: Mapping[str, Any]) -> str:
    header = tomlwrite.comment_header(
        f"os2slice config, written by its config page (/admin), os2slice {__version__}.\n"
        "Comments aren't kept; os2slice.example.toml has a commented example."
    )
    return header + "\n" + tomlwrite.dumps(data, inline_keys={"profiles", "extra"})


def _save(
    svc: Service,
    mutate: Callable[[dict[str, Any]], None],
    what: str,
    secrets: Mapping[str, str] | None = None,
) -> config.Config:
    """Edit, validate, write and reload. `Invalid` means nothing was written or stored."""
    secrets = dict(secrets or {})
    with _SAVE_LOCK:
        data = _raw(svc)
        mutate(data)
        new = _parse(data, svc)
        try:
            trial = svc.build_modules(new, overlay=secrets)
        except Os2sliceError as e:
            raise Invalid(e.one_line()) from e
        trial.close()
        for name, value in secrets.items():
            try:
                auth.store_secret(name, value)
            except Os2sliceError as e:
                raise Invalid(e.one_line()) from e
            log.warning("admin: secret %s stored", name)
        path = svc.cfg.path
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
        tomlwrite.write_atomic(path, render_toml(data), mode=mode)
        log.warning("admin: config saved (%s) to %s", what, path)
    try:
        return svc.reload()
    except Os2sliceError as e:
        raise Invalid(f"Saved, but the service couldn't reload it: {e.one_line()}") from e


def _has_secret(svc: Service, name: str) -> bool:
    try:
        return bool(svc.secret(name))
    except Os2sliceError:
        return False


def _check_secret_value(label: str, value: str) -> str:
    value = value.strip()
    if any(c.isspace() for c in value) or len(value) > 4096:
        raise Invalid(f"{label} can't contain spaces")
    return value


# ---------------------------------------------------------------------------
# Login, logout, password


def _login_page(ctx: Ctx, error: str = "", status: int = 200) -> None:
    inner = _msg(error, "bad") + _text(
        "password", "Admin password", kind="password", attrs=' autocomplete="current-password"'
    )
    _send_page(ctx.h, status, "Config page: sign in", ctx.form(LOGIN, inner, "Sign in"))


def _post_login(ctx: Ctx) -> None:
    h, store = ctx.h, ctx.svc.admin
    form = _read_form(h)
    _expect(form, {"password"})
    if not ctx.redeem(LOGIN, form.get("csrf", "")):
        raise Refused("This form has expired or was already used", "Reload the page")
    peer = str(h.client_address[0])
    if not store.attempt_allowed(peer):
        wait = max(1, math.ceil(store.retry_after(peer)))
        body = (
            f"<p><b>Too many wrong passwords.</b> Try again in {wait} seconds.</p>"
            f'<p><a href="{LOGIN}">Sign in</a></p>'
        )
        _send_page(h, 429, "Config page: wait", body, {"Retry-After": str(wait)})
        return
    if not store.verify(form.get("password", "")):
        store.record_failure(peer)
        _login_page(ctx, "Wrong password.", 401)
        return
    store.record_success(peer)
    _set_session_cookie(h, store.create_session(), SESSION_TTL)
    log.warning("admin: signed in from %s", peer)
    _redirect(h, "/admin")


def _post_logout(ctx: Ctx, form: dict[str, str]) -> None:
    _expect(form, ())
    ctx.svc.admin.revoke(ctx.token)
    _set_session_cookie(ctx.h, "", 0)
    ctx.redirect(LOGIN)


def _get_password(ctx: Ctx, params: dict[str, str], error: str = "", status: int = 200) -> None:
    pw = ' autocomplete="new-password"'
    inner = (
        _msg(error, "bad")
        + _text("current", "Current password", kind="password",
                attrs=' autocomplete="current-password"')
        + _text("new", "New password (at least 12 characters)", kind="password", attrs=pw)
        + _text("repeat", "Repeat the new password", kind="password", attrs=pw)
    )  # fmt: skip
    body = (
        _msg("Password changed; other admin sessions were signed out.", "good")
        if "saved" in params
        else ""
    ) + (
        "<p>Changing the password signs every admin session out (this one gets a new one). "
        "Setting a forgotten one: <code>os2slice admin-password</code> on the server.</p>"
        + ctx.form("/admin/password", inner, "Change password")
    )
    ctx.page("Admin password", body, status, "/admin/password")


def _post_password(ctx: Ctx, form: dict[str, str]) -> None:
    _expect(form, {"current", "new", "repeat"})
    store, peer = ctx.svc.admin, str(ctx.h.client_address[0])
    if not store.attempt_allowed(peer):
        _get_password(ctx, {}, "Too many wrong passwords; wait and try again.", 429)
        return
    if not store.verify(form.get("current", "")):
        store.record_failure(peer)
        _get_password(ctx, {}, "The current password is wrong.", 401)
        return
    store.record_success(peer)
    if form.get("new", "") != form.get("repeat", ""):
        _get_password(ctx, {}, "The new passwords don't match.", 400)
        return
    try:
        store.set_password(form.get("new", ""))
    except PasswordError as e:
        _get_password(ctx, {}, f"Not changed: {e}.", 400)
        return
    log.warning("admin: password changed from the config page by %s", peer)
    _set_session_cookie(ctx.h, store.create_session(), SESSION_TTL)
    ctx.redirect("/admin/password?saved=1")


# ---------------------------------------------------------------------------
# Overview


def _get_overview(ctx: Ctx, params: dict[str, str]) -> None:
    from os2slice import cli

    svc = ctx.svc
    rows: list[tuple[str, str, str]] = []

    def add(status: str, check: str, detail: str) -> None:
        rows.append((status, check, detail))

    try:
        config.load(svc.cfg.path, create=False)
        add(cli.PASS, "config", str(svc.cfg.path))
    except Os2sliceError as e:
        add(cli.FAIL, "config", e.one_line())
    cli.check_onshape(svc.file_cfg, False, add)
    cli.check_module_secrets(svc.file_cfg, add)
    cli.check_module_health(svc.modules, add)
    add(cli.PASS, "admin password", "set")
    add(cli.PASS, "log", str(log_path()))
    css = {cli.PASS: "ok", cli.FAIL: "bad", cli.WARN: "warn"}
    table = "".join(
        f'<tr><td class="{css.get(s, "")}">{_e(s)}</td><td>{_e(c)}</td><td>{_e(d)}</td></tr>'
        for s, c, d in rows
    )
    s = svc.cfg.server
    body = f"""<dl>
<dt>Version</dt><dd>os2slice {_e(__version__)}</dd>
<dt>Config file</dt><dd>{_e(svc.cfg.path)}</dd>
<dt>Identity</dt><dd>{_e(s.identity)}</dd>
<dt>Listening</dt><dd>{"https" if s.tls_cert else "http"}://{_e(s.bind)}:{s.port}</dd>
<dt>Hosts</dt><dd>{_e(", ".join(s.hosts))}</dd>
</dl><h2>Checks</h2><div class="scroll"><table>
<tr><th>Status</th><th>Check</th><th>Detail</th></tr>{table}</table></div>"""
    ctx.page("Config", body, here="/admin")


# ---------------------------------------------------------------------------
# Slicers and targets (modules)


def _kinds_for(section: str) -> dict[str, ModuleSpec]:
    roles = ("slicer",) if section == "slicers" else ("target", "both")
    return {k: s for k, s in registry.kinds().items() if s.role in roles}


def _entry_kind(section: str, entry: Mapping[str, Any]) -> str:
    default = "desktop" if section == "slicers" else ""
    return str(entry.get("kind", default))


def _health(svc: Service, section: str, key: str, entry: Mapping[str, Any]) -> tuple[bool, str]:
    kind = _entry_kind(section, entry)
    if kind == "desktop":
        from os2slice.slicers import resolve_executable

        argv = entry.get("argv") or [""]
        exe = resolve_executable(str(argv[0])) if argv[0] else None
        return (exe is not None, f"found: {exe}" if exe else f"{argv[0]!s} not found here")
    pool = svc.modules.targets if section == "targets" else svc.modules.slicers
    module = pool.get(key)
    if module is None:
        return False, "not loaded"
    try:
        h = module.check()
    except Os2sliceError as e:
        return False, e.one_line()
    return h.ok, h.summary + (f" ({h.detail})" if h.detail else "")


def _summary(section: str, key: str, spec: ModuleSpec | None, entry: Mapping[str, Any],
             svc: Service) -> str:  # fmt: skip
    if spec is None:
        return ""
    parts = []
    for f in spec.fields:
        if f.type == "secret":
            name = registry.secret_name(section, key, f.key)
            parts.append(f"{f.label}: {'set' if _has_secret(svc, name) else 'not set'}")
        elif f.key in entry:
            v = entry[f.key]
            shown = " ".join(map(str, v)) if isinstance(v, list) else str(v)
            parts.append(f"{f.key} = {shown[:120]}")
    if isinstance(entry.get("models"), dict) and entry["models"]:
        parts.append(f"models: {', '.join(map(str, entry['models']))}")
    return "<br>".join(_e(p) for p in parts)


def _get_modules(ctx: Ctx, section: str, params: dict[str, str], error: str = "",
                 status: int = 200) -> None:  # fmt: skip
    svc = ctx.svc
    data = _raw(svc)
    entries = data.get(section, {})
    entries = entries if isinstance(entries, dict) else {}
    kinds = registry.kinds()
    rows = []
    for key, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        kind = _entry_kind(section, entry)
        spec = kinds.get(kind)
        ok, text = _health(svc, section, key, entry)
        edit = f"/admin/{section}/edit?{urlencode({'key': key})}"
        remove = ctx.form(f"/admin/{section}/remove",
                          f'<input type="hidden" name="key" value="{_e(key)}">', "Remove",
                          cls="small")  # fmt: skip
        rows.append(
            f'<tr><td><a href="{_e(edit)}">{_e(key)}</a></td>'
            f"<td>{_e(spec.label if spec else kind)}</td>"
            f"<td>{_summary(section, key, spec, entry, svc)}</td>"
            f'<td class="{"ok" if ok else "bad"}">{_e(text)}</td><td>{remove}</td></tr>'
        )
    legacy = ""
    if section == "targets" and isinstance(data.get("bambuddy"), dict):
        ok, text = _health(svc, "targets", "bambuddy", {"kind": "bambuddy"})
        bb = data["bambuddy"]
        presets = bb.get("presets") if isinstance(bb.get("presets"), dict) else {}
        summary = f"url = {bb.get('base_url', '')}; presets for {', '.join(presets) or 'none'}"
        migrate = ctx.form("/admin/targets/migrate", "", "Migrate", cls="small")
        legacy = (
            "<tr><td>bambuddy (legacy table)</td><td>BamBuddy</td>"
            f'<td>{_e(summary)}</td><td class="{"ok" if ok else "bad"}">{_e(text)}</td>'
            f"<td>{migrate}</td></tr>"
        )
        legacy_note = (
            "<p class=muted>The legacy <code>[bambuddy]</code> table works as "
            "<code>[targets.bambuddy]</code>. Migrate rewrites it as that (with its presets "
            "as per-model defaults) so it can be edited here; it behaves the same.</p>"
        )
    else:
        legacy_note = ""
    choices = [(k, f"{s.label} ({k})") for k, s in _kinds_for(section).items()]
    add = (
        f'<form method="get" action="/admin/{section}/new"><label>Add a '
        f'{"slicer" if section == "slicers" else "target"}<select name="kind">'
        f'{_opts(choices, "")}</select></label><button type="submit">Next</button></form>'
    )
    both = (
        "<p class=muted>A target that slices for itself (BamBuddy) is configured once, under "
        "Targets, and is also a slicer by its name.</p>"
        if section == "slicers"
        else ""
    )
    table = (
        '<div class="scroll"><table><tr><th>Name</th><th>Kind</th><th>Settings</th>'
        f"<th>Health</th><th></th></tr>{legacy}{''.join(rows)}</table></div>"
        if rows or legacy
        else f"<p>No {section} are configured.</p>"
    )
    body = _saved(params) + _msg(error, "bad") + both + table + legacy_note + add
    ctx.page(section.capitalize(), body, status, f"/admin/{section}")


def _vals_from_entry(spec: ModuleSpec, entry: Mapping[str, Any]) -> dict[str, str]:
    vals: dict[str, str] = {}
    for f in spec.fields:
        if f.type == "secret":
            continue
        v = entry.get(f.key, f.default)
        name = f"f.{f.key}"
        if f.type == "bool":
            vals[name] = "on" if v else ""
        elif f.type == "list":
            vals[name] = "\n".join(map(str, v or []))
        else:
            vals[name] = "" if v is None else str(v)
    models = entry.get("models")
    if isinstance(models, dict):
        for n, (model, m) in enumerate(models.items()):
            m = m if isinstance(m, dict) else {}
            prof = m.get("profiles") if isinstance(m.get("profiles"), dict) else {}
            vals[f"m{n}.model"] = str(model)
            vals[f"m{n}.slicer"] = str(m.get("slicer", ""))
            for k in ("printer", "process", "filament"):
                vals[f"m{n}.{k}"] = str(prof.get(k, ""))
            vals[f"m{n}.bed_type"] = str(m.get("bed_type", "") or "")
    return vals


def _field_input(f: Field, vals: Mapping[str, str], section: str, key: str,
                 svc: Service) -> str:  # fmt: skip
    name = f"f.{f.key}"
    v = vals.get(name, "")
    if f.type == "secret":
        is_set = bool(key) and _has_secret(svc, registry.secret_name(section, key, f.key))
        req = " (required)" if f.required else ""
        return _secret_input(f"s.{f.key}", f.label + req, is_set, f.help)
    if f.type == "bool":
        return _check(name, f.label, bool(v), f.help)
    if f.type == "list":
        return _area(name, f.label + " (one per line)", v, f.help, f.required)
    if f.type == "choice":
        choices = ([] if f.required else [("", "(default)")]) + [(c, c) for c in f.choices]
        return _select(name, f.label, choices, v, f.help)
    kind = "number" if f.type == "int" else "text"
    return _text(name, f.label, v, help=f.help, required=f.required, kind=kind)


def _slicer_choices(svc: Service, target_role: str = "") -> list[tuple[str, str]]:
    cfg = svc.file_cfg
    first = "(the target slices for itself)" if target_role == "both" else "(none)"
    out = [("", first)]
    out += [(k, f"{k} ({registry.spec_for(m.kind).label})") for k, m in cfg.slicer_modules.items()]
    out += [
        (k, f"{k} ({registry.spec_for(t.kind).label})")
        for k, t in cfg.targets.items()
        if registry.spec_for(t.kind).role == "both"
    ]
    return out


def _catalog(svc: Service, slicer: str, model: str = "") -> ProfileCatalog | None:
    module = svc.modules.slicers.get(slicer)
    if module is None:
        return None
    try:
        return module.profiles(model)  # type: ignore[no-any-return]
    except Exception as e:  # a read for suggestions only; the form works without it
        log.info("profiles() of %s failed: %s", slicer, type(e).__name__)
        return None


def _models_table(svc: Service, key: str, spec: ModuleSpec, vals: Mapping[str, str]) -> str:
    rows_n = sorted({int(m.group(1)) for k in vals if (m := MODEL_ROW_RE.fullmatch(k))})
    rows_n = [*rows_n[: MAX_MODEL_ROWS - 1], max(rows_n, default=-1) + 1]  # one blank row
    slicers = _slicer_choices(svc, spec.role)
    catalog = _catalog(svc, key) if spec.role == "both" else None
    lists = ""
    if catalog:
        lists = (
            _datalist("dl-printer", catalog.printer)
            + _datalist("dl-process", catalog.process)
            + _datalist("dl-filament", catalog.filament)
        )

    def cell(n: int, field: str) -> str:
        v = vals.get(f"m{n}.{field}", "")
        dl = f' list="dl-{field}"' if catalog and field != "model" else ""
        return f'<td><input type="text" name="m{n}.{field}" value="{_e(v)}"{dl}></td>'

    rows = []
    for n in rows_n:
        slicer = vals.get(f"m{n}.slicer", "")
        bed = vals.get(f"m{n}.bed_type", "")
        rows.append(
            f"<tr>{cell(n, 'model')}"
            f'<td><select name="m{n}.slicer">{_opts(slicers, slicer)}</select></td>'
            f"{cell(n, 'printer')}{cell(n, 'process')}{cell(n, 'filament')}"
            f'<td><select name="m{n}.bed_type">{_opts(_plate_choices(), bed)}</select></td></tr>'
        )
    return (
        "<h2>Defaults per printer model</h2><p class=muted>For the printers this target "
        "finds by itself: which slicer and profiles each model uses. Clear a model's name "
        "to remove its row; the last row adds one.</p>"
        f'{lists}<div class="scroll"><table><tr><th>Model</th><th>Slicer</th>'
        "<th>Printer profile</th><th>Process</th><th>Filament</th><th>Plate</th></tr>"
        f"{''.join(rows)}</table></div>"
    )


def _module_form(ctx: Ctx, section: str, spec: ModuleSpec, key: str, vals: Mapping[str, str],
                 *, new: bool, error: str = "", result: str = "", ok: bool = False,
                 status: int = 200) -> None:  # fmt: skip
    svc = ctx.svc
    action = f"/admin/{section}/save"
    head = (
        f'<input type="hidden" name="kind" value="{_e(spec.kind)}">'
        f'<input type="hidden" name="mode" value="{"new" if new else "edit"}">'
    )
    if new:
        head += _text("key", "Name", vals.get("key", ""), required=True,
                      help="a-z, 0-9, _ and -; printers refer to it by this name")  # fmt: skip
    else:
        head += f'<input type="hidden" name="key" value="{_e(key)}">'
    fields = "".join(_field_input(f, vals, section, "" if new else key, svc) for f in spec.fields)
    models = (
        _models_table(svc, key, spec, vals)
        if section == "targets" and spec.discovers_printers
        else ""
    )
    test = (
        ""
        if spec.kind == "desktop"
        else '<button type="submit" name="do" value="test" class="second">Test connection</button>'
    )
    form = (
        f'<form method="post" action="{_e(action)}">{ctx.csrf(action)}{head}{fields}{models}'
        f'<div class="buttons"><button type="submit" name="do" value="save">Save</button>'
        f"{test}</div></form>"
    )
    title = f"{'Add' if new else 'Edit'} {spec.label}" + ("" if new else f": {key}")
    intro = f"<p class=muted>{_e(spec.help)}</p>" if spec.help else ""
    res = _msg(f"Test connection: {result}", "good" if ok else "bad") if result else ""
    ctx.page(title, intro + _msg(error, "bad") + res + form, status, f"/admin/{section}")


def _get_module_new(ctx: Ctx, section: str, params: dict[str, str]) -> None:
    spec = _kinds_for(section).get(params.get("kind", ""))
    if spec is None:
        raise BadRequest("Unknown module kind", f"Pick one on /admin/{section}")
    _module_form(ctx, section, spec, "", _vals_from_entry(spec, {}), new=True)


def _get_module_edit(ctx: Ctx, section: str, params: dict[str, str]) -> None:
    key = params.get("key", "")
    entries = _raw(ctx.svc).get(section, {})
    entry = entries.get(key) if isinstance(entries, dict) else None
    if not isinstance(entry, dict):
        raise BadRequest(f"No [{section}.{key[:40]}] in the config")
    spec = registry.kinds().get(_entry_kind(section, entry))
    if spec is None:
        raise BadRequest(f"[{section}.{key}] has an unknown kind; fix it by hand")
    _module_form(ctx, section, spec, key, _vals_from_entry(spec, entry), new=False)


def _entry_from_form(section: str, spec: ModuleSpec, form: Mapping[str, str],
                     old: Mapping[str, Any]) -> dict[str, Any]:  # fmt: skip
    entry: dict[str, Any] = {} if spec.kind == "desktop" else {"kind": spec.kind}
    for f in spec.fields:
        if f.type == "secret":
            continue
        raw = form.get(f"f.{f.key}", "")
        if f.type == "bool":
            entry[f.key] = raw != ""
        elif f.type == "int":
            s = raw.strip()
            if s:
                if not re.fullmatch(r"-?[0-9]{1,9}", s):
                    raise Invalid(f"{f.label} must be a whole number")
                entry[f.key] = int(s)
        elif f.type == "list":
            items = _lines(raw)
            if items:
                entry[f.key] = items
        else:
            s = raw.strip()
            if s:
                entry[f.key] = s
    if section == "targets" and spec.discovers_printers:
        old_models = old.get("models") if isinstance(old.get("models"), dict) else {}
        models = _models_from_form(form, old_models)
        if models:
            entry["models"] = models
    elif "models" in old:
        entry["models"] = old["models"]
    return entry


def _models_from_form(form: Mapping[str, str], old: Mapping[str, Any]) -> dict[str, Any]:
    rows = sorted({int(m.group(1)) for k in form if (m := MODEL_ROW_RE.fullmatch(k))})
    models: dict[str, Any] = {}
    for n in rows[:MAX_MODEL_ROWS]:

        def get(field: str) -> str:
            return form.get(f"m{n}.{field}", "").strip()  # noqa: B023 - used in this loop

        model = get("model")
        if not model:
            continue
        if not TEXT_RE.fullmatch(model):
            raise Invalid("Model names are one line of text, up to 200 characters")
        if model in models:
            raise Invalid(f"Model {model!r} is listed twice")
        m: dict[str, Any] = {}
        if get("slicer"):
            m["slicer"] = get("slicer")
        prof = {k: get(k) for k in ("printer", "process", "filament") if get(k)}
        if prof:
            m["profiles"] = prof
        if get("bed_type"):
            m["bed_type"] = get("bed_type")
        prev = old.get(model)
        if isinstance(prev, dict) and "extra" in prev:
            m["extra"] = prev["extra"]  # module-specific (preset_source), kept as it was
        models[model] = m
    return models


def _module_names(spec: ModuleSpec) -> set[str]:
    names = {"do", "key", "kind", "mode"}
    for f in spec.fields:
        names.add(f"{'s' if f.type == 'secret' else 'f'}.{f.key}")
    return names


def _post_module_save(ctx: Ctx, section: str, form: dict[str, str]) -> None:
    svc = ctx.svc
    spec = _kinds_for(section).get(form.get("kind", ""))
    if spec is None:
        raise BadRequest("Unknown module kind")
    discovers = section == "targets" and spec.discovers_printers
    _expect(form, _module_names(spec), MODEL_ROW_RE if discovers else None)
    new = form.get("mode") == "new"
    key = form.get("key", "").strip()
    vals = {k: v for k, v in form.items() if not k.startswith("s.") and k not in ("do", "mode")}
    try:
        if not SLICER_KEY_RE.fullmatch(key):
            raise Invalid("Names may only use a-z, 0-9, _ and - (up to 32)")
        secrets = {
            registry.secret_name(section, key, f.key): _check_secret_value(f.label, v)
            for f in spec.fields
            if f.type == "secret" and (v := form.get(f"s.{f.key}", "").strip())
        }

        def mutate(data: dict[str, Any]) -> None:
            tbl = _table(data, section)
            if new and key in tbl:
                raise Invalid(f"[{section}.{key}] already exists; pick another name")
            if not new and key not in tbl:
                raise Invalid(f"[{section}.{key}] is gone from the file; reload the page")
            old = tbl.get(key) if isinstance(tbl.get(key), dict) else {}
            tbl[key] = _entry_from_form(section, spec, form, old)

        if form.get("do") == "test":
            ok, text = _test_connection(svc, section, key, spec, mutate, secrets)
            _module_form(ctx, section, spec, key, vals, new=new, result=text, ok=ok)
            return
        _save(svc, mutate, f"{section}.{key}", secrets)
    except Invalid as e:
        _module_form(ctx, section, spec, key, vals, new=new, error=e.message, status=400)
        return
    ctx.redirect(f"/admin/{section}?saved=1")


def _test_connection(svc: Service, section: str, key: str, spec: ModuleSpec,
                     mutate: Callable[[dict[str, Any]], None],
                     secrets: Mapping[str, str]) -> tuple[bool, str]:  # fmt: skip
    """Build this one module from the submitted values and run `check()`; nothing is saved."""
    if spec.kind == "desktop":
        return False, "desktop slicers run on the desktop; `os2slice doctor` checks them there"
    data = _raw(svc)
    mutate(data)
    cfg = _parse(data, svc)
    mc = (cfg.targets if section == "targets" else cfg.slicer_modules).get(key)
    if mc is None:
        return False, "not configured"

    def lookup(name: str) -> str | None:
        return secrets.get(name) or svc.secret(name)

    try:
        values = registry.resolve_secrets(section, key, spec, mc.values, lookup)
        transport = svc.transport_for(key)
        if section == "targets":
            module: Any = registry.build_target(
                mc.kind, {**values, "models": mc.models}, key=key, transport=transport
            )
        else:
            module = registry.build_slicer(mc.kind, values, key=key, transport=transport)
        try:
            health = module.check()
        finally:
            close = getattr(module, "close", None)
            if callable(close):
                close()
    except Os2sliceError as e:
        return False, e.one_line()
    text = health.summary + (f" ({health.detail})" if health.detail else "")
    return health.ok, text


def _post_module_remove(ctx: Ctx, section: str, form: dict[str, str]) -> None:
    _expect(form, {"key"})
    key = form.get("key", "")

    def mutate(data: dict[str, Any]) -> None:
        tbl = _table(data, section)
        if key not in tbl:
            raise Invalid(f"[{section}.{key[:40]}] isn't in the file")
        del tbl[key]
        if not tbl:
            del data[section]

    try:
        _save(ctx.svc, mutate, f"remove {section}.{key}")
    except Invalid as e:
        _get_modules(ctx, section, {}, f"Not removed: {e.message}", 400)
        return
    ctx.redirect(f"/admin/{section}?saved=1")


def migrate_legacy(data: dict[str, Any]) -> None:
    """Rewrite `[bambuddy]` as `[targets.bambuddy]` (+ models), as config.py reads it."""
    bb = data.pop("bambuddy", None)
    if not isinstance(bb, dict):
        raise Invalid("There's no [bambuddy] table to migrate")
    targets = data.get("targets", {})
    if not isinstance(targets, dict) or "bambuddy" in targets:
        raise Invalid("[targets.bambuddy] already exists; remove one of them by hand")
    t: dict[str, Any] = {"kind": "bambuddy"}
    if "base_url" in bb:
        t["url"] = str(bb["base_url"]).rstrip("/")
    for k in ("folder", "manual_start"):
        if k in bb:
            t[k] = bb[k]
    if bb.get("public_url"):
        t["public_url"] = str(bb["public_url"]).rstrip("/")
    models: dict[str, Any] = {}
    presets = bb.get("presets", {})
    for model, p in (presets if isinstance(presets, dict) else {}).items():
        if not isinstance(p, dict):
            raise Invalid(f"[bambuddy.presets.{model}] isn't a table; fix it by hand")
        m: dict[str, Any] = {
            "profiles": {k: p[k] for k in ("printer", "process", "filament") if k in p}
        }
        if p.get("bed_type"):
            m["bed_type"] = p["bed_type"]
        m["extra"] = {"preset_source": p.get("source", "standard")}
        models[model] = m
    if models:
        t["models"] = models
    if bb.get("default_printer"):
        if data.get("default_printer"):
            raise Invalid("default_printer is set twice; remove one by hand")
        data["default_printer"] = bb["default_printer"]
    data["targets"] = {"bambuddy": t, **targets}


def _post_migrate(ctx: Ctx, form: dict[str, str]) -> None:
    _expect(form, ())
    try:
        _save(ctx.svc, migrate_legacy, "migrate [bambuddy]")
    except Invalid as e:
        _get_modules(ctx, "targets", {}, f"Not migrated: {e.message}", 400)
        return
    ctx.redirect("/admin/targets?saved=1")


# ---------------------------------------------------------------------------
# Printers

PRINTER_NAMES = {
    "mode", "key", "target", "slicer", "name", "model", "technology", "bed_w", "bed_d",
    "nozzle_count", "p.printer", "p.process", "p.filament", "materials", "bed_type",
}  # fmt: skip


def _num(v: object) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _printer_vals(key: str, entry: Mapping[str, Any]) -> dict[str, str]:
    prof = entry.get("profiles") if isinstance(entry.get("profiles"), dict) else {}
    bed = entry.get("bed_mm") if isinstance(entry.get("bed_mm"), list) else []
    return {
        "key": key,
        **{k: str(entry.get(k, "") or "") for k in ("target", "slicer", "name", "model")},
        "technology": str(entry.get("technology", "")),
        "bed_w": _num(bed[0]) if len(bed) == 2 else "",
        "bed_d": _num(bed[1]) if len(bed) == 2 else "",
        "nozzle_count": str(entry.get("nozzle_count", "")),
        **{f"p.{k}": str(prof.get(k, "")) for k in ("printer", "process", "filament")},
        "materials": "\n".join(map(str, entry.get("materials", []) or [])),
        "bed_type": str(entry.get("bed_type", "") or ""),
    }


def _get_printers(ctx: Ctx, params: dict[str, str], error: str = "", status: int = 200) -> None:
    svc = ctx.svc
    data = _raw(svc)
    entries = data.get("printers", {})
    entries = entries if isinstance(entries, dict) else {}
    rows = []
    for key, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        v = _printer_vals(key, entry)
        prof = " / ".join(x for x in (v["p.printer"], v["p.process"], v["p.filament"]) if x)
        edit = f"/admin/printers/edit?{urlencode({'key': key})}"
        remove = ctx.form("/admin/printers/remove",
                          f'<input type="hidden" name="key" value="{_e(key)}">', "Remove",
                          cls="small")  # fmt: skip
        rows.append(
            f'<tr><td><a href="{_e(edit)}">{_e(key)}</a></td>'
            f"<td>{_e(v['target'] or '(override)')}</td><td>{_e(v['slicer'])}</td>"
            f"<td>{_e(v['model'])}</td><td>{_e(prof)}</td><td>{remove}</td></tr>"
        )
    configured = (
        '<div class="scroll"><table><tr><th>Name</th><th>Target</th><th>Slicer</th>'
        f"<th>Model</th><th>Profiles</th><th></th></tr>{''.join(rows)}</table></div>"
        if rows
        else "<p>No printers are configured by hand.</p>"
    )
    try:
        found = svc.modules.printers()
        found_err = ""
    except Os2sliceError as e:
        found, found_err = [], e.one_line()
    disc = []
    for p in found:
        if p.key in entries:
            continue
        link = (
            f"/admin/printers/edit?{urlencode({'key': p.name})}"
            if p.name in entries
            else f"/admin/printers/new?{urlencode({'name': p.name})}"
        )
        label = "Edit override" if p.name in entries else "Override"
        disc.append(
            f"<tr><td>{_e(p.name)}</td><td>{_e(p.key)}</td><td>{_e(p.model)}</td>"
            f"<td>{_e(p.target)}</td><td>{_e(p.slicer or '(none)')}</td>"
            f"<td>{'yes' if p.active else 'no'}</td>"
            f'<td><a href="{_e(link)}">{label}</a></td></tr>'
        )
    discovered = _msg(found_err, "bad") + (
        '<div class="scroll"><table><tr><th>Name</th><th>Key</th><th>Model</th><th>Target</th>'
        f"<th>Slicer</th><th>Active</th><th></th></tr>{''.join(disc)}</table></div>"
        if disc
        else "<p>None.</p>"
    )
    names = sorted({p.name for p in found} | set(entries))
    default = str(svc.file_cfg.default_printer)
    dp_form = ctx.form(
        "/admin/printers/default",
        _select("default_printer", "Default printer",
                [("", "(none)")] + [(n, n) for n in names], default),
    )  # fmt: skip
    body = (
        _saved(params) + _msg(error, "bad") + "<h2>Configured</h2>" + configured
        + '<p><a href="/admin/printers/new">Add a printer</a></p>'
        + "<h2>Found by targets</h2><p class=muted>Read-only; Override creates a "
        "<code>[printers.&quot;name&quot;]</code> entry with settings for that printer.</p>"
        + discovered + "<h2>Default</h2>" + dp_form
    )  # fmt: skip
    ctx.page("Printers", body, status, "/admin/printers")


def _printer_form(ctx: Ctx, vals: Mapping[str, str], *, new: bool, error: str = "",
                  status: int = 200) -> None:  # fmt: skip
    svc = ctx.svc
    cfg = svc.file_cfg
    key = vals.get("key", "")
    targets = [("", "(none: settings for a printer a target finds, matched by name)")]
    targets += [(k, f"{k} ({registry.spec_for(t.kind).label})") for k, t in cfg.targets.items()]
    slicer = vals.get("slicer", "")
    if not slicer:  # suggestions from the slicer a found printer of this name uses
        slicer = next((p.slicer for p in _found(svc) if p.name == key and p.slicer), "")
    catalog = _catalog(svc, slicer, vals.get("model", "")) if slicer else None
    lists = ""
    if catalog:
        lists = (
            _datalist("dl-printer", catalog.printer)
            + _datalist("dl-process", catalog.process)
            + _datalist("dl-filament", catalog.filament)
        )

    def prof(k: str, label: str) -> str:
        attr = f' list="dl-{k}"' if catalog else ""
        return _text(f"p.{k}", label, vals.get(f"p.{k}", ""), attrs=attr)

    head = f'<input type="hidden" name="mode" value="{"new" if new else "edit"}">'
    if new:
        head += _text("key", "Name", key, required=True,
                      help="for a found printer: its name exactly, e.g. X1C_01")  # fmt: skip
    else:
        head += f'<input type="hidden" name="key" value="{_e(key)}">'
    inner = (
        head
        + _select("target", "Target", targets, vals.get("target", ""))
        + _select("slicer", "Slicer", _slicer_choices(svc), vals.get("slicer", ""),
                  "required with a target that doesn't slice")
        + _text("name", "Display name", vals.get("name", ""), help="default: the name above")
        + _text("model", "Model", vals.get("model", ""))
        + _select("technology", "Technology", [("", "(the target's)"), ("fdm", "FDM"),
                                               ("sla", "SLA")], vals.get("technology", ""))
        + '<div class="row">'
        + _text("bed_w", "Bed width (mm)", vals.get("bed_w", ""), kind="number",
                attrs=' step="any"')
        + _text("bed_d", "Bed depth (mm)", vals.get("bed_d", ""), kind="number",
                attrs=' step="any"')
        + _text("nozzle_count", "Nozzles", vals.get("nozzle_count", ""), kind="number")
        + "</div>" + lists
        + prof("printer", "Printer profile") + prof("process", "Process profile")
        + prof("filament", "Filament profile")
        + _area("materials", "Materials (one per line)", vals.get("materials", ""),
                "for targets that can't say what's loaded")
        + _select("bed_type", "Build plate", _plate_choices(), vals.get("bed_type", ""))
    )  # fmt: skip
    title = "Add a printer" if new else f"Edit printer {key}"
    hint = "" if catalog else (
        "<p class=muted>Profile names are free text here; with a slicer chosen and saved, "
        "its known names are suggested.</p>"
    )  # fmt: skip
    body = _msg(error, "bad") + hint + ctx.form("/admin/printers/save", inner)
    ctx.page(title, body, status, "/admin/printers")


def _found(svc: Service) -> list[Any]:
    try:
        return svc.modules.printers()
    except Os2sliceError:
        return []


def _get_printer_new(ctx: Ctx, params: dict[str, str]) -> None:
    name = params.get("name", "")
    vals = {"key": name if config.PRINTER_KEY_RE.fullmatch(name) else ""}
    _printer_form(ctx, vals, new=True)


def _get_printer_edit(ctx: Ctx, params: dict[str, str]) -> None:
    key = params.get("key", "")
    entries = _raw(ctx.svc).get("printers", {})
    entry = entries.get(key) if isinstance(entries, dict) else None
    if not isinstance(entry, dict):
        raise BadRequest(f'No [printers."{key[:40]}"] in the config')
    _printer_form(ctx, _printer_vals(key, entry), new=False)


def _bed(w: str, d: str) -> list[int | float] | None:
    w, d = w.strip(), d.strip()
    if not w and not d:
        return None
    try:
        dims = [float(w), float(d)]
    except ValueError as e:
        raise Invalid("Bed width and depth must be numbers (mm)") from e
    if not all(0 < x <= 10_000 and math.isfinite(x) for x in dims):
        raise Invalid("Bed width and depth must be between 0 and 10000 mm")
    return [int(x) if x.is_integer() else x for x in dims]


def _printer_entry(form: Mapping[str, str], old: Mapping[str, Any]) -> dict[str, Any]:
    entry: dict[str, Any] = {}
    for k in ("target", "slicer", "name", "model", "technology"):
        v = form.get(k, "").strip()
        if v:
            entry[k] = v
    bed = _bed(form.get("bed_w", ""), form.get("bed_d", ""))
    if bed:
        entry["bed_mm"] = bed
    nozzles = form.get("nozzle_count", "").strip()
    if nozzles:
        if not re.fullmatch(r"[0-9]{1,2}", nozzles):
            raise Invalid("Nozzles must be a whole number from 1 to 16")
        entry["nozzle_count"] = int(nozzles)
    prof = {
        k: v for k in ("printer", "process", "filament") if (v := form.get(f"p.{k}", "").strip())
    }
    if prof:
        entry["profiles"] = prof
    materials = _lines(form.get("materials", ""))
    if materials:
        entry["materials"] = materials
    if form.get("bed_type", ""):
        entry["bed_type"] = form["bed_type"]
    if "extra" in old:
        entry["extra"] = old["extra"]  # module-specific, kept as it was
    return entry


def _post_printer_save(ctx: Ctx, form: dict[str, str]) -> None:
    _expect(form, PRINTER_NAMES)
    new = form.get("mode") == "new"
    key = form.get("key", "").strip()
    vals = {k: v for k, v in form.items() if k != "mode"}
    try:
        if not config.PRINTER_KEY_RE.fullmatch(key):
            raise Invalid("Printer names use letters, digits, spaces and _.()+- (up to 64)")

        def mutate(data: dict[str, Any]) -> None:
            tbl = _table(data, "printers")
            if new and key in tbl:
                raise Invalid(f'[printers."{key}"] already exists; pick another name')
            if not new and key not in tbl:
                raise Invalid(f'[printers."{key}"] is gone from the file; reload the page')
            old = tbl.get(key) if isinstance(tbl.get(key), dict) else {}
            tbl[key] = _printer_entry(form, old)

        _save(ctx.svc, mutate, f"printers.{key}")
    except Invalid as e:
        _printer_form(ctx, vals, new=new, error=e.message, status=400)
        return
    ctx.redirect("/admin/printers?saved=1")


def _post_printer_remove(ctx: Ctx, form: dict[str, str]) -> None:
    _expect(form, {"key"})
    key = form.get("key", "")

    def mutate(data: dict[str, Any]) -> None:
        tbl = _table(data, "printers")
        if key not in tbl:
            raise Invalid(f'[printers."{key[:40]}"] isn\'t in the file')
        del tbl[key]
        if not tbl:
            del data["printers"]

    try:
        _save(ctx.svc, mutate, f"remove printers.{key}")
    except Invalid as e:
        _get_printers(ctx, {}, f"Not removed: {e.message}", 400)
        return
    ctx.redirect("/admin/printers?saved=1")


def _post_default_printer(ctx: Ctx, form: dict[str, str]) -> None:
    _expect(form, {"default_printer"})
    value = form.get("default_printer", "").strip()

    def mutate(data: dict[str, Any]) -> None:
        if value and not TEXT_RE.fullmatch(value):
            raise Invalid("A printer name is one line of text")
        legacy = data.get("bambuddy")
        if isinstance(legacy, dict) and "default_printer" in legacy:
            legacy["default_printer"] = value  # it lives there in a legacy config
        elif value:
            data["default_printer"] = value
        else:
            data.pop("default_printer", None)

    try:
        _save(ctx.svc, mutate, "default_printer")
    except Invalid as e:
        _get_printers(ctx, {}, e.message, 400)
        return
    ctx.redirect("/admin/printers?saved=1")


# ---------------------------------------------------------------------------
# Onshape, server, print defaults


def _onshape_vals(cfg: config.Config) -> dict[str, str]:
    return {
        "base_url": cfg.onshape_base_url,
        "auth": cfg.onshape_auth,
        "oauth_client_id": cfg.oauth_client_id,
        "oauth_url": cfg.oauth_base_url,
    }


def _keys_set() -> bool:
    try:
        auth.load_keys()
    except Os2sliceError:
        return False
    return True


def _get_onshape(ctx: Ctx, params: dict[str, str], vals: Mapping[str, str] | None = None,
                 error: str = "", status: int = 200) -> None:  # fmt: skip
    v = vals or _onshape_vals(ctx.svc.file_cfg)
    inner = (
        _text("base_url", "Onshape URL", v.get("base_url", ""), required=True,
              help="https://<name>.onshape.com")
        + _select("auth", "Reading documents", [("keys", "keys: one shared API key pair"),
                  ("oauth", "oauth: each user signs in with Onshape (D-23)")], v.get("auth", ""))
        + _text("oauth_client_id", "OAuth client id", v.get("oauth_client_id", ""),
                help='needed with "oauth"')
        + _text("oauth_url", "OAuth URL", v.get("oauth_url", ""),
                help="default https://oauth.onshape.com")
        + _secret_input("oauth_client_secret", "OAuth client secret",
                        auth.has_oauth_client_secret())
        + _secret_input("access_key", "API access key", _keys_set(),
                        "the access and secret key are saved together")
        + _secret_input("secret_key", "API secret key", _keys_set())
    )  # fmt: skip
    body = (
        _saved(params) + _msg(error, "bad")
        + "<p class=muted>Onshape settings and keys take effect when the service restarts.</p>"
        + ctx.form("/admin/onshape", inner)
    )  # fmt: skip
    ctx.page("Onshape", body, status, "/admin/onshape")


def _post_onshape(ctx: Ctx, form: dict[str, str]) -> None:
    _expect(form, {"base_url", "auth", "oauth_client_id", "oauth_url", "oauth_client_secret",
                   "access_key", "secret_key"})  # fmt: skip
    svc = ctx.svc
    vals = {k: form.get(k, "") for k in ("base_url", "auth", "oauth_client_id", "oauth_url")}
    try:
        access = _check_secret_value("The access key", form.get("access_key", ""))
        secret = _check_secret_value("The secret key", form.get("secret_key", ""))
        client_secret = _check_secret_value(
            "The client secret", form.get("oauth_client_secret", "")
        )
        if bool(access) != bool(secret):
            raise Invalid("Give both API keys, or neither")

        def mutate(data: dict[str, Any]) -> None:
            tbl = _table(data, "onshape")
            for k, v in vals.items():
                v = v.strip()
                if v:
                    tbl[k] = v
                else:
                    tbl.pop(k, None)

        _save(svc, mutate, "onshape")
        if access:
            auth.store_keys(access, secret)
            svc.restart_notes.add("New Onshape API keys were saved: restart the service.")
            log.warning("admin: Onshape API keys stored")
        if client_secret:
            auth.store_oauth_client_secret(client_secret)
            svc.restart_notes.add("A new OAuth client secret was saved: restart the service.")
            log.warning("admin: OAuth client secret stored")
    except Invalid as e:
        _get_onshape(ctx, {}, vals, e.message, 400)
        return
    except Os2sliceError as e:
        _get_onshape(ctx, {}, vals, e.one_line(), 400)
        return
    ctx.redirect("/admin/onshape?saved=1")


SERVER_NAMES = {
    "bind", "port", "hosts", "identity", "allowed_users", "tls_cert", "tls_key", "redirect_port",
}  # fmt: skip


def _server_vals(cfg: config.Config) -> dict[str, str]:
    s = cfg.server
    return {
        "bind": s.bind,
        "port": str(s.port),
        "hosts": "\n".join(s.hosts),
        "identity": s.identity,
        "allowed_users": "\n".join(s.allowed_users),
        "tls_cert": str(s.tls_cert or ""),
        "tls_key": str(s.tls_key or ""),
        "redirect_port": str(s.redirect_port or ""),
    }


def _get_server(ctx: Ctx, params: dict[str, str], vals: Mapping[str, str] | None = None,
                error: str = "", status: int = 200) -> None:  # fmt: skip
    v = vals or _server_vals(ctx.svc.file_cfg)
    inner = (
        _text("bind", "Bind address", v.get("bind", ""), help="127.0.0.1, or 0.0.0.0 with lan")
        + _text("port", "Port", v.get("port", ""), kind="number")
        + _area("hosts", "Host names browsers use (host:port, one per line)",
                v.get("hosts", ""), "requests with any other Host header are refused")
        + _select("identity", "Who may use it", [("none", "none: this computer only"),
                  ("lan", "lan: anyone on the LAN, HTTPS (D-17)"),
                  ("tailscale", "tailscale: listed Tailscale logins")], v.get("identity", ""))
        + _area("allowed_users", "Tailscale logins (one per line)", v.get("allowed_users", ""))
        + _text("tls_cert", "TLS certificate (PEM chain)", v.get("tls_cert", ""))
        + _text("tls_key", "TLS private key", v.get("tls_key", ""))
        + _text("redirect_port", "Redirect port (plain HTTP)", v.get("redirect_port", ""),
                kind="number", help="lan only: redirects to the HTTPS /admin page; empty = off")
    )  # fmt: skip
    body = (
        _saved(params) + _msg(error, "bad")
        + "<p class=muted>These take effect when the service restarts.</p>"
        + ctx.form("/admin/server", inner)
    )  # fmt: skip
    ctx.page("Server", body, status, "/admin/server")


def _post_server(ctx: Ctx, form: dict[str, str]) -> None:
    _expect(form, SERVER_NAMES)
    vals = {k: form.get(k, "") for k in SERVER_NAMES}

    def mutate(data: dict[str, Any]) -> None:
        port = vals["port"].strip()
        if not re.fullmatch(r"[0-9]{1,5}", port):
            raise Invalid("The port must be a whole number")
        tbl: dict[str, Any] = {
            "bind": vals["bind"].strip(),
            "port": int(port),
            "hosts": _lines(vals["hosts"]),
            "identity": vals["identity"].strip(),
        }
        users = _lines(vals["allowed_users"])
        if users:
            tbl["allowed_users"] = users
        for k in ("tls_cert", "tls_key"):
            if vals[k].strip():
                tbl[k] = vals[k].strip()
        redirect_port = vals["redirect_port"].strip()
        if redirect_port and redirect_port != "0":
            if not re.fullmatch(r"[0-9]{1,5}", redirect_port):
                raise Invalid("The redirect port must be a whole number, or empty for off")
            tbl["redirect_port"] = int(redirect_port)
        data["server"] = tbl

    try:
        _save(ctx.svc, mutate, "server")
    except Invalid as e:
        _get_server(ctx, {}, vals, e.message, 400)
        return
    ctx.redirect("/admin/server?saved=1")


PRINT_SETTING_NAMES = {
    "walls", "infill", "supports", "build_plate_only", "top_layers", "bottom_layers", "brim",
    "copies",
}  # fmt: skip
DEFAULT_NAMES = {
    "walls", "infill", "supports", "build_plate_only", "top_layers", "bottom_layers", "brim",
    "copies", "bed_type",
}  # fmt: skip


def _defaults_vals(cfg: config.Config) -> dict[str, str]:
    p = cfg.print_defaults
    return {
        "walls": str(p.walls),
        "infill": str(p.infill),
        "supports": p.supports,
        "build_plate_only": "on" if p.build_plate_only else "",
        "top_layers": str(p.top_layers),
        "bottom_layers": str(p.bottom_layers),
        "brim": "on" if p.brim else "",
        "copies": str(p.copies),
        "bed_type": cfg.default_bed_type or "",
    }


def _get_defaults(ctx: Ctx, params: dict[str, str], vals: Mapping[str, str] | None = None,
                  error: str = "", status: int = 200) -> None:  # fmt: skip
    v = vals or _defaults_vals(ctx.svc.file_cfg)

    def num(name: str, label: str, rng: tuple[int, int]) -> str:
        return _text(name, f"{label} ({rng[0]} to {rng[1]})", v.get(name, ""), kind="number",
                     attrs=f' min="{rng[0]}" max="{rng[1]}"')  # fmt: skip

    inner = (
        num("walls", "Wall loops", WALLS_RANGE) + num("infill", "Infill %", INFILL_RANGE)
        + _select("supports", "Supports", [(s, s) for s in SUPPORTS], v.get("supports", ""))
        + _check("build_plate_only", "Supports from the build plate only",
                 bool(v.get("build_plate_only")))
        + num("top_layers", "Top layers", SHELL_RANGE)
        + num("bottom_layers", "Bottom layers", SHELL_RANGE)
        + _check("brim", "Brim", bool(v.get("brim")))
        + num("copies", "Copies", COPIES_RANGE)
        + _select("bed_type", "Build plate", _plate_choices(), v.get("bed_type", ""),
                  "a printer's or model's own plate wins")
    )  # fmt: skip
    body = (
        _saved(params) + _msg(error, "bad")
        + "<p class=muted>Where the print page and panel start; people change them per print.</p>"
        + ctx.form("/admin/defaults", inner)
    )  # fmt: skip
    ctx.page("Print defaults", body, status, "/admin/defaults")


def _post_defaults(ctx: Ctx, form: dict[str, str]) -> None:
    _expect(form, DEFAULT_NAMES)
    vals = {k: form.get(k, "") for k in DEFAULT_NAMES}
    try:
        strings = {k: v for k, v in vals.items() if k in PRINT_SETTING_NAMES}
        try:
            s = PrintSettings.from_strings(strings, PrintSettings())
        except BadRequest as e:
            raise Invalid(e.message) from e

        def mutate(data: dict[str, Any]) -> None:
            tbl: dict[str, Any] = {
                "walls": s.walls,
                "infill": s.infill,
                "supports": s.supports,
                "build_plate_only": s.build_plate_only,
                "top_layers": s.top_layers,
                "bottom_layers": s.bottom_layers,
                "brim": s.brim,
                "copies": s.copies,
            }
            if vals["bed_type"]:
                tbl["bed_type"] = vals["bed_type"]
            data["print_defaults"] = tbl

        _save(ctx.svc, mutate, "print_defaults")
    except Invalid as e:
        _get_defaults(ctx, {}, vals, e.message, 400)
        return
    ctx.redirect("/admin/defaults?saved=1")


# ---------------------------------------------------------------------------
# The Onshape panel: its "Open in …" links and extra settings ([panel])

PANEL_NAMES = {"local_slicer", "web_slicer", *(f"extra_{key}" for key in extra_settings.BY_KEY)}


def _panel_vals(cfg: config.Config, raw: Mapping[str, Any]) -> dict[str, str]:
    return {
        "local_slicer": cfg.panel_local_slicer or "none",
        "web_slicer": str(raw.get("web_slicer", "")),
        **{f"extra_{key}": "on" for key in cfg.panel_extras},
    }


def _get_panel_settings(ctx: Ctx, params: dict[str, str], vals: Mapping[str, str] | None = None,
                        error: str = "", status: int = 200) -> None:  # fmt: skip
    cfg = ctx.svc.file_cfg
    v = vals or _panel_vals(cfg, _table(_raw(ctx.svc), "panel"))
    local = [(name, label) for name, label in config.DESKTOP_SLICERS.items()] + [("none", "None")]
    web: list[tuple[str, str]] = [("", "Every one that is set up")]
    missing = []
    for name, label in config.DESKTOP_SLICERS.items():
        ws = getattr(cfg, config.WEB_SLICER_TABLES[name])
        if ws is None:
            missing.append(f"{label} ([{config.WEB_SLICER_TABLES[name]}])")
        else:
            web.append((name, f"{label}, {ws.url}"))
    web.append(("none", "None"))
    note = f"Not set up here: {', '.join(missing)}." if missing else ""
    inner = (
        "<h2>Open in a slicer</h2>"
        + _select("local_slicer", "On this computer", local, v.get("local_slicer", ""),
                  "the slicer installed on each user's own computer; the link uses its URL "
                  "handler")
        + _select("web_slicer", "In the browser", web, v.get("web_slicer", ""),
                  f"the shared slicer session(s) on this server. {note}".strip())
        + "<h2>More settings</h2>"
        + "<p class=muted>Each one ticked appears in the panel, empty: the profile's own value "
          "applies until someone fills it in. Filament settings go into every filament of the "
          "print; BamBuddy printers can't take them.</p>"
        + "".join(
            _check(f"extra_{x.key}", f"{x.label}{f' ({x.unit})' if x.unit else ''}",
                   bool(v.get(f"extra_{x.key}")),
                   f"{x.scope} setting {x.key}" + (f"; {x.help}" if x.help else ""))
            for x in extra_settings.CATALOG
        )
    )  # fmt: skip
    body = _saved(params) + _msg(error, "bad") + ctx.form("/admin/panel", inner)
    ctx.page("Onshape panel", body, status, "/admin/panel")


def _post_panel_settings(ctx: Ctx, form: dict[str, str]) -> None:
    _expect(form, PANEL_NAMES)
    vals = {k: form.get(k, "") for k in PANEL_NAMES}
    try:

        def mutate(data: dict[str, Any]) -> None:
            panel = dict(data.get("panel") or {})
            panel["local_slicer"] = vals["local_slicer"] or "bambu-studio"
            if vals["web_slicer"]:
                panel["web_slicer"] = vals["web_slicer"]
            else:
                panel.pop("web_slicer", None)
            chosen = [x.key for x in extra_settings.CATALOG if vals.get(f"extra_{x.key}")]
            if chosen:
                panel["extra_settings"] = chosen
            else:
                panel.pop("extra_settings", None)
            data["panel"] = panel

        _save(ctx.svc, mutate, "panel")
    except Invalid as e:
        _get_panel_settings(ctx, {}, vals, e.message, 400)
        return
    ctx.redirect("/admin/panel?saved=1")


# ---------------------------------------------------------------------------
# Jobs and log


def _get_jobs(ctx: Ctx, params: dict[str, str]) -> None:
    jobs = ctx.svc.jobs.recent(JOB_ROWS)
    rows = []
    for j in jobs:
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(j.created))
        last = j.error or (j.steps[-1] if j.steps else "")
        rows.append(
            f"<tr><td>{_e(when)}</td><td>{_e(j.user)}</td><td>{_e(j.title)}</td>"
            f"<td>{_e(j.state)}</td><td>{len(j.steps)} steps; {_e(last)}</td></tr>"
        )
    body = (
        "<p class=muted>Every user's jobs since the service started (kept in memory).</p>"
        '<div class="scroll"><table><tr><th>Started</th><th>User</th><th>Job</th>'
        f"<th>State</th><th>Steps</th></tr>{''.join(rows)}</table></div>"
        if rows
        else "<p>No jobs since the service started.</p>"
    )
    ctx.page("Jobs", body, here="/admin/jobs")


def log_tail(lines: int = LOG_LINES, max_bytes: int = 256_000) -> list[str]:
    """The last `lines` lines of the log (only its last `max_bytes` are read)."""
    path = log_path()
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace")
    out = text.splitlines()
    if size > max_bytes and out:
        out = out[1:]  # the first line is probably cut
    return out[-lines:]


def _get_log(ctx: Ctx, params: dict[str, str]) -> None:
    lines = log_tail()
    body = (
        f"<p class=file>{_e(log_path())}, last {len(lines)} lines</p>"
        f'<pre class="log">{_e(chr(10).join(lines))}</pre>'
        if lines
        else f"<p>{_e(log_path())} is empty or missing.</p>"
    )
    ctx.page("Log", body, here="/admin/log")


# ---------------------------------------------------------------------------
# Secrets


def implied_secrets(data: Mapping[str, Any]) -> list[tuple[str, str]]:
    """(secret name, label) for every secret field the config's modules have."""
    kinds = registry.kinds()
    out: list[tuple[str, str]] = []
    for section in ("targets", "slicers"):
        tbl = data.get(section, {})
        for key, entry in (tbl if isinstance(tbl, dict) else {}).items():
            if not isinstance(entry, dict):
                continue
            spec = kinds.get(_entry_kind(section, entry))
            for f in spec.fields if spec else ():
                if f.type == "secret":
                    name = registry.secret_name(section, key, f.key)
                    out.append((name, f"{spec.label} {key}: {f.label}"))  # type: ignore[union-attr]
    if isinstance(data.get("bambuddy"), dict):
        out.insert(0, (auth.BAMBUDDY_SECRET, "BamBuddy (legacy table): API key"))
    return out


def _get_secrets(ctx: Ctx, params: dict[str, str], error: str = "", status: int = 200) -> None:
    svc = ctx.svc
    names = implied_secrets(_raw(svc))
    rows = "".join(
        f"<tr><td><code>{_e(n)}</code></td><td>{_e(label)}</td>"
        f"<td>{'set' if _has_secret(svc, n) else 'not set'}</td></tr>"
        for n, label in names
    )
    onshape = (
        f"<tr><td>Onshape API keys</td><td>shared keys</td>"
        f"<td>{'set' if _keys_set() else 'not set'}</td></tr>"
        f"<tr><td>Onshape OAuth client secret</td><td>per-user sign-in</td>"
        f"<td>{'set' if auth.has_oauth_client_secret() else 'not set'}</td></tr>"
    )
    form = (
        ctx.form(
            "/admin/secrets",
            _select("name", "Secret", [(n, n) for n, _ in names], "")
            + _secret_input("value", "Value", False),
            "Save secret",
        )
        if names
        else ""
    )
    body = (
        _saved(params) + _msg(error, "bad")
        + "<p class=muted>Secrets live in the secret store (keyring, or the environment), "
        "never in config.toml, and are never shown. Onshape keys are set on the Onshape "
        "page.</p>"
        + '<div class="scroll"><table><tr><th>Name</th><th>For</th><th>State</th></tr>'
        + onshape + rows + "</table></div>" + form
    )  # fmt: skip
    ctx.page("Secrets", body, status, "/admin/secrets")


def _post_secrets(ctx: Ctx, form: dict[str, str]) -> None:
    _expect(form, {"name", "value"})
    svc = ctx.svc
    name = form.get("name", "")
    try:
        if name not in {n for n, _ in implied_secrets(_raw(svc))}:
            raise Invalid("Pick a secret from the list")
        value = _check_secret_value("The value", form.get("value", ""))
        if not value:
            raise Invalid("Enter the value")
        try:
            auth.store_secret(name, value)
        except Os2sliceError as e:
            raise Invalid(e.one_line()) from e
        log.warning("admin: secret %s stored", name)
        try:
            svc.reload()
        except Os2sliceError as e:
            raise Invalid(f"Saved, but the service couldn't reload: {e.one_line()}") from e
    except Invalid as e:
        _get_secrets(ctx, {}, e.message, 400)
        return
    ctx.redirect("/admin/secrets?saved=1")


# ---------------------------------------------------------------------------
# Routes

SAVED = ("saved",)
GET_ROUTES: dict[str, tuple[Get, tuple[str, ...]]] = {
    "/admin": (_get_overview, ()),
    "/admin/slicers": (lambda c, p: _get_modules(c, "slicers", p), SAVED),
    "/admin/slicers/new": (lambda c, p: _get_module_new(c, "slicers", p), ("kind",)),
    "/admin/slicers/edit": (lambda c, p: _get_module_edit(c, "slicers", p), ("key",)),
    "/admin/targets": (lambda c, p: _get_modules(c, "targets", p), SAVED),
    "/admin/targets/new": (lambda c, p: _get_module_new(c, "targets", p), ("kind",)),
    "/admin/targets/edit": (lambda c, p: _get_module_edit(c, "targets", p), ("key",)),
    "/admin/printers": (_get_printers, SAVED),
    "/admin/printers/new": (_get_printer_new, ("name",)),
    "/admin/printers/edit": (_get_printer_edit, ("key",)),
    "/admin/onshape": (_get_onshape, SAVED),
    "/admin/server": (_get_server, SAVED),
    "/admin/defaults": (_get_defaults, SAVED),
    "/admin/panel": (_get_panel_settings, SAVED),
    "/admin/jobs": (_get_jobs, ()),
    "/admin/log": (_get_log, ()),
    "/admin/secrets": (_get_secrets, SAVED),
    "/admin/password": (_get_password, SAVED),
}
POST_ROUTES: dict[str, Post] = {
    "/admin/logout": _post_logout,
    "/admin/password": _post_password,
    "/admin/slicers/save": lambda c, f: _post_module_save(c, "slicers", f),
    "/admin/slicers/remove": lambda c, f: _post_module_remove(c, "slicers", f),
    "/admin/targets/save": lambda c, f: _post_module_save(c, "targets", f),
    "/admin/targets/remove": lambda c, f: _post_module_remove(c, "targets", f),
    "/admin/targets/migrate": _post_migrate,
    "/admin/printers/save": _post_printer_save,
    "/admin/printers/remove": _post_printer_remove,
    "/admin/printers/default": _post_default_printer,
    "/admin/onshape": _post_onshape,
    "/admin/server": _post_server,
    "/admin/defaults": _post_defaults,
    "/admin/panel": _post_panel_settings,
    "/admin/secrets": _post_secrets,
}
