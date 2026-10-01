"""`os2slice serve`: the confirmation page and print jobs (Phase 4).

Security rules (docs/ARCHITECTURE.md, D-11, D-13):
- Every request: Host must be one of server.hosts; with identity = "tailscale",
  Tailscale-User-Login must be in server.allowed_users.
- GET never uploads, slices or prints. It only reads to build the page.
- POST /print needs a single-use CSRF token from that page, Sec-Fetch-Site:
  same-origin and a matching Origin. Only then does a job start.
"""

from __future__ import annotations

import functools
import hashlib
import html
import http.cookies
import json
import logging
import os
import re
import secrets
import ssl
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx

from os2slice import __version__, files, printing
from os2slice.bambuddy import BambuddyClient, PresetChoice, Printer
from os2slice.config import Config, WebStudioConfig
from os2slice.errors import AuthError, BadRequest, ConfigError, Os2sliceError
from os2slice.filaments import (
    NOZZLE_NAMES,
    Slot,
    match_preset,
    preset_suffix,
    slots_from_status,
)
from os2slice.jobs import CsrfTokens, Job, JobStore
from os2slice.oauth import (
    SESSION_DAYS,
    BearerAuth,
    Grant,
    GrantStore,
    OAuthSettings,
    SignIns,
    TokenEndpoint,
    UserTokens,
)
from os2slice.oauth import static_bearer as oauth_static_bearer
from os2slice.onshape import OnshapeClient
from os2slice.orientation import FACE_ID_RE, Orientation
from os2slice.request import PART_ID_RE, ExportRequest, parse_print_query
from os2slice.settings import (
    COPIES_RANGE,
    PLATE_LABELS,
    SHELL_RANGE,
    SUPPORTS,
    PrintSettings,
    check_bed_type,
)

log = logging.getLogger(__name__)

MAX_BODY = 16_384
REQUEST_FIELDS = ("d", "wv", "wvid", "e", "p", "c")
FORM_FIELDS = frozenset(
    {
        *REQUEST_FIELDS,
        "csrf",
        "claim",  # /auth/claim (D-23)
        "printer",
        "filament",  # the panel's own filament menu: a global tray id, "" = preset
        "orient",
        "walls",
        "infill",
        "supports",
        "build_plate_only",
        "top_layers",
        "bottom_layers",
        "brim",
        "copies",
        "face",
        "plate",
        "extra",
    }
)
CHECKBOXES = ("build_plate_only", "brim")  # an unchecked box isn't sent at all
EXTRA_RE = re.compile(r"([A-Za-z0-9_+\-]{1,32}):(\d{1,3})")
MAX_PARTS = 16
ORIENT_CHOICES = [
    ("as-modeled", "As modeled (bottom face down)"),
    ("auto", "Let the slicer choose (auto-orient)"),
    ("z+", "Top (+Z) face down"),
    ("x+", "+X side down"),
    ("x-", "-X side down"),
    ("y+", "+Y side down"),
    ("y-", "-Y side down"),
]
CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; form-action 'self'; "
    "base-uri 'none'"
)
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    # same-origin, not no-referrer: with no-referrer browsers send `Origin: null` on our
    # own form POST, which the Origin check (rightly) refuses.
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",
}
STATIC_FILES = {  # the only files /static/ serves
    "panel.js",
    "preview.js",
    "auth.js",
    "vendor/three.module.js",
    "vendor/three.core.js",
    "vendor/STLLoader.js",
    "vendor/OrbitControls.js",
}
MODEL_PATH_RE = re.compile(r"/models/([A-Za-z0-9_-]{22})/os2slice\.3mf")
MODEL_LINK_TTL = 900  # seconds a Bambu Studio download link stays valid
KNOWN_PATHS = frozenset(
    {"/print", "/health", "/panel", "/panel/print", "/panel/preview", "/panel/model-link"}
    | {"/panel/web-studio", "/panel/web-studio/status"}
    | {"/auth/start", "/auth/callback", "/auth/claim", "/auth/sign-out"}
    | {f"/static/{f}" for f in STATIC_FILES}
)
PREVIEW_FIELDS = frozenset({*REQUEST_FIELDS, "face", "orient", "extra", "only"})
# Approximate build plates (mm) for the preview; the slicer has the real numbers.
BED_MM = {
    "A1 Mini": (180, 180),
    "A1": (256, 256),
    "X1C": (256, 256),
    "X1": (256, 256),
    "X1E": (256, 256),
    "P1S": (256, 256),
    "P1P": (256, 256),
    "P2S": (256, 256),
    "H2D": (350, 320),
    "H2D Pro": (350, 320),
    "H2S": (340, 320),
    "H2C": (350, 320),
}
JOB_PATH_RE = re.compile(r"/jobs/([A-Za-z0-9_-]{22})")

OnshapeFactory = Callable[[], OnshapeClient]
OnshapeAsFactory = Callable[[httpx.Auth], OnshapeClient]  # a signed-in user's client (D-23)
BambuddyFactory = Callable[[], BambuddyClient]
SESSION_COOKIE = "os2s"  # top-level pages: the /print tab and the sign-in window
PANEL_COOKIE = "os2s_p"  # the panel inside Onshape (partitioned third-party cookie)
STATE_COOKIE = "os2s_state"
SESSION_MAX_AGE = SESSION_DAYS * 86400
NEXT_RE = re.compile(r"/(print|panel)\?[A-Za-z0-9%=&._~+-]{0,1500}")


class Forbidden(Os2sliceError):
    exit_code = 2
    http_status = 403


class NotSignedIn(AuthError):
    http_status = 401


@dataclass
class SignIn:
    """Per-user Onshape sign-in (D-23); None on the Service in API-key mode."""

    store: GrantStore
    tokens: UserTokens
    endpoint: TokenEndpoint
    pending: SignIns
    onshape_as: OnshapeAsFactory


def make_signin(
    cfg: Config,
    client_secret: str,
    store_path: Path,
    onshape_as: OnshapeAsFactory,
    transport: httpx.BaseTransport | None = None,
) -> SignIn:
    """Wire up per-user sign-in for `[onshape] auth = "oauth"` (D-23)."""
    settings = OAuthSettings(
        cfg.oauth_client_id, client_secret, cfg.oauth_redirect_uri, cfg.oauth_base_url
    )
    endpoint = TokenEndpoint(settings, transport)
    store = GrantStore(store_path)
    return SignIn(store, UserTokens(store, endpoint), endpoint, SignIns(), onshape_as)


class Service:
    """Everything a request handler needs; built once per process."""

    def __init__(
        self,
        cfg: Config,
        onshape: OnshapeFactory,
        bambuddy: BambuddyFactory,
        signin: SignIn | None = None,
    ) -> None:
        self.cfg = cfg
        self.onshape = onshape
        self.bambuddy = bambuddy
        self.signin = signin
        self.jobs = JobStore()
        self.tokens = CsrfTokens()
        self.previews = PreviewCache()
        self.model_links: OrderedDict[str, tuple[float, tuple[Any, ...], bytes | None]] = (
            OrderedDict()
        )
        self.model_lock = threading.Lock()
        self._preset_names: tuple[float, list[str]] = (0.0, [])
        self._preset_lock = threading.Lock()

    def onshape_for(self, user_id: str | None) -> OnshapeClient:
        """API-key mode: the shared client. Sign-in mode: that user's own access."""
        if self.signin is None:
            return self.onshape()
        if user_id is None:
            raise NotSignedIn("Sign in with Onshape first", "Use the Sign in button in the panel")
        return self.signin.onshape_as(BearerAuth(self.signin.tokens, user_id))

    def preset_names(self, bb: BambuddyClient) -> list[str]:
        """BamBuddy's filament preset names, cached for 10 minutes."""
        with self._preset_lock:
            stamp, names = self._preset_names
            if time.monotonic() - stamp < 600 and names:
                return names
        names = bb.filament_preset_names()
        with self._preset_lock:
            self._preset_names = (time.monotonic(), names)
        return names


def make_handler(service: Service) -> type[BaseHTTPRequestHandler]:
    class Handler(_Handler):
        svc = service

    return Handler


def serve(service: Service) -> None:
    httpd = make_server(service)
    s = service.cfg.server
    scheme = "https" if s.tls_cert else "http"
    log.info("serving on %s://%s:%s (hosts %s, identity %s)", scheme, s.bind, s.port, s.hosts,
             s.identity)  # fmt: skip
    print(f"os2slice serving on {scheme}://{s.bind}:{s.port} for {', '.join(s.hosts)}")
    httpd.serve_forever()


def make_server(service: Service, port: int | None = None) -> ThreadingHTTPServer:
    s = service.cfg.server
    httpd = ThreadingHTTPServer((s.bind, s.port if port is None else port), make_handler(service))
    if s.tls_cert and s.tls_key:
        tls = TlsCertificate(s.tls_cert, s.tls_key)
        # Handshake lazily in the handler thread, so a slow client can't stall accept().
        httpd.socket = tls.context.wrap_socket(
            httpd.socket, server_side=True, do_handshake_on_connect=False
        )
        threading.Thread(target=tls.watch, daemon=True).start()
    return httpd


class TlsCertificate:
    """Loads the certificate, and reloads it when DuckDNS renews the files (D-17)."""

    def __init__(self, cert: Path, key: Path) -> None:
        self.cert, self.key = cert, key
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2
        self._stamp = self._mtimes()
        self.context.load_cert_chain(cert, key)

    def _mtimes(self) -> tuple[float, float]:
        return (self.cert.stat().st_mtime, self.key.stat().st_mtime)

    def reload_if_changed(self) -> bool:
        stamp = self._mtimes()
        if stamp == self._stamp:
            return False
        self.context.load_cert_chain(self.cert, self.key)  # applies to new connections
        self._stamp = stamp
        log.info("reloaded TLS certificate %s", self.cert)
        return True

    def watch(self, every: float = 3600.0) -> None:
        while True:
            time.sleep(every)
            try:
                self.reload_if_changed()
            except (OSError, ssl.SSLError) as e:
                log.error("TLS certificate reload failed (keeping the old one): %s", e)


class _Handler(BaseHTTPRequestHandler):
    svc: Service
    server_version = f"os2slice/{__version__}"
    sys_version = ""

    # -- dispatch ----------------------------------------------------------

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._method_not_allowed()

    do_DELETE = do_PATCH = do_PUT

    def do_HEAD(self) -> None:
        self._method_not_allowed()

    def _dispatch(self, method: str) -> None:
        url = urlsplit(self.path)
        user = "?"
        self._frame_origin = ""  # set by pages Onshape may frame (the panel)
        self._scripts = False
        self._cache = ""  # overrides Cache-Control (static files, previews)
        self._cookies: list[str] = []
        self._who: Grant | None = None  # signed-in Onshape user (D-23)
        try:
            if url.path == "/favicon.ico" and method == "GET":
                self._send(204, b"")
                return
            user = self._check_host_and_user()
            if self.svc.signin is not None:
                self._who = self._signed_in_user()
                if self._who is not None:
                    user = f"onshape:{self._who.user_id}"
            if url.path == "/health" and method == "GET":
                self._send(200, f'{{"ok": true, "version": "{__version__}"}}\n'.encode(),
                           "application/json")  # fmt: skip
            elif url.path == "/print" and method == "GET":
                self._get_print(url.query, user)
            elif url.path == "/print" and method == "POST":
                self._post_print(user, panel=False)
            elif url.path == "/auth/start" and method == "GET":
                self._get_auth_start(url.query)
            elif url.path == "/auth/callback" and method == "GET":
                self._get_auth_callback(url.query)
            elif url.path == "/auth/claim" and method == "POST":
                self._post_auth_claim()
            elif url.path == "/auth/sign-out" and method == "POST":
                self._post_sign_out()
            elif url.path == "/panel" and method == "GET":
                self._get_panel(url.query, user)
            elif url.path == "/panel/print" and method == "POST":
                self._post_print(user, panel=True)
            elif url.path == "/panel/web-studio" and method == "POST":
                self._post_web_studio()
            elif url.path == "/panel/web-studio/status" and method == "GET":
                self._get_web_studio_status()
            elif url.path == "/panel/model-link" and method == "POST":
                self._post_model_link()
            elif (m := MODEL_PATH_RE.fullmatch(url.path)) and method == "GET":
                self._get_model(m.group(1))
            elif url.path == "/panel/preview" and method == "GET":
                self._get_preview(url.query)
            elif url.path.startswith("/static/") and method == "GET":
                self._get_static(url.path.removeprefix("/static/"))
            elif (m := JOB_PATH_RE.fullmatch(url.path)) and method == "GET":
                self._get_job(m.group(1), user)
            elif url.path in KNOWN_PATHS or JOB_PATH_RE.fullmatch(url.path):
                self._method_not_allowed()
            else:
                self._page(404, "Not found", "<p>Nothing here.</p>")
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError) as e:
            # The browser stopped listening, e.g. the panel cancelled a preview download
            # when the selection changed; nothing more can be sent.
            log.info("%s %s: client went away (%s)", method, url.path, type(e).__name__)
        except Os2sliceError as e:
            log.warning("%s %s by %s: %s", method, url.path, user, e.one_line())
            self._error_page(e)
        except Exception:
            log.exception("%s %s by %s crashed", method, url.path, user)
            self._page(500, "Unexpected error", "<p>Something went wrong; see the server log.</p>")

    # -- checks ------------------------------------------------------------

    def _check_host_and_user(self) -> str:
        cfg = self.svc.cfg.server
        host = self.headers.get("Host", "")
        if host not in cfg.hosts:
            raise Forbidden(f"Wrong host {host[:60]!r}")
        if cfg.identity == "none":
            return "local"
        if cfg.identity == "lan":
            return "lan"  # open to the LAN by design (D-17)
        login = self.headers.get("Tailscale-User-Login", "")
        if login not in cfg.allowed_users:
            raise Forbidden("You're not allowed to use this service", "Ask Josh to add your login")
        return login

    def _uid(self) -> str | None:
        return self._who.user_id if self._who else None

    def _onshape(self) -> OnshapeClient:
        return self.svc.onshape_for(self._uid())

    def _require_navigation(self) -> None:
        dest = self.headers.get("Sec-Fetch-Dest")
        if dest != "document":
            raise Forbidden(
                "Open this page from Onshape's menu", "It can't be loaded inside another page"
            )

    def _require_same_origin_post(self) -> None:
        if self.headers.get("Sec-Fetch-Site") != "same-origin":
            raise Forbidden("Cross-site form submission refused")
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if origin is not None and urlsplit(origin).netloc != host:
            raise Forbidden("Form came from another site")

    # -- per-user Onshape sign-in (D-23) ------------------------------------

    def _cookie(self, name: str) -> str:
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
        except http.cookies.CookieError:
            return ""
        morsel = jar.get(name)
        return morsel.value if morsel else ""

    def _set_cookie(self, name: str, value: str, max_age: int, *, panel: bool = False,
                    path: str = "/") -> None:  # fmt: skip
        # The panel lives in Onshape's iframe: its cookie must be SameSite=None and is
        # Partitioned (CHIPS), so it works with third-party cookies otherwise blocked.
        site = "SameSite=None; Partitioned" if panel else "SameSite=Lax"
        self._cookies.append(
            f"{name}={value}; Path={path}; Max-Age={max_age}; Secure; HttpOnly; {site}"
        )

    def _signed_in_user(self) -> Grant | None:
        store = self.svc.signin.store  # type: ignore[union-attr]
        for name in (PANEL_COOKIE, SESSION_COOKIE):
            sid = self._cookie(name)
            if sid and (grant := store.session_user(sid)) is not None:
                return grant
        return None

    def _signin(self) -> SignIn:
        if self.svc.signin is None:
            raise ConfigError(
                "Sign-in isn't enabled on this server", 'Set [onshape] auth = "oauth"'
            )
        return self.svc.signin

    def _get_auth_start(self, query: str) -> None:
        """Top-level (popup or tab): remember where to go back, send the user to Onshape."""
        signin = self._signin()
        self._require_navigation()
        params = dict(parse_qsl(query, max_num_fields=2))
        next_path = params.get("next", "")
        if not NEXT_RE.fullmatch(next_path):
            next_path = ""
        state, nonce = signin.pending.start(next_path)
        # Binds the answer to this browser (no signing someone else in via a crafted link).
        self._set_cookie(STATE_COOKIE, nonce, 600, path="/auth/")
        settings = signin.endpoint.settings
        self.send_response(303)
        self.send_header("Location", settings.authorize_url(state))
        self._headers(0)

    def _get_auth_callback(self, query: str) -> None:
        signin = self._signin()
        self._require_navigation()
        params = dict(parse_qsl(query, max_num_fields=6))
        if "error" in params:
            raise NotSignedIn("Onshape sign-in was cancelled", "Try Sign in again")
        next_path = signin.pending.finish(params.get("state", ""), self._cookie(STATE_COOKIE))
        if next_path is None:
            raise Forbidden("This sign-in link has expired or isn't yours", "Try Sign in again")
        code = params.get("code", "")
        if not re.fullmatch(r"[A-Za-z0-9._~+/=-]{1,512}", code):
            raise BadRequest("Onshape sent back no sign-in code")
        tokens = signin.endpoint.exchange_code(code)
        with signin.onshape_as(oauth_static_bearer(tokens["access_token"])) as onshape:
            uid, name = onshape.whoami()
        signin.tokens.save_new(uid, name, tokens)
        sid = signin.store.new_session(uid)
        self._set_cookie(SESSION_COOKIE, sid, SESSION_MAX_AGE)
        self._set_cookie(STATE_COOKIE, "", 0, path="/auth/")
        claim = signin.pending.offer_claim(sid)
        log.warning("signed in: Onshape user %s", uid)
        self._scripts = True
        self._page(200, "Signed in", _signed_in_page(name, claim, next_path))

    def _post_auth_claim(self) -> None:
        """The panel (iframe) picks up the session its sign-in window just made."""
        signin = self._signin()
        self._require_same_origin_post()
        sid = signin.pending.redeem_claim(self._read_form().get("claim", ""))
        if sid is None or signin.store.session_user(sid) is None:
            raise Forbidden("That sign-in has expired", "Try Sign in again")
        self._set_cookie(PANEL_COOKIE, sid, SESSION_MAX_AGE, panel=True)
        self._send(200, b'{"ok": true}', "application/json")

    def _post_sign_out(self) -> None:
        """Ends this browser's sessions and forgets the user's Onshape grant."""
        signin = self._signin()
        self._require_same_origin_post()
        for name in (PANEL_COOKIE, SESSION_COOKIE):
            if sid := self._cookie(name):
                signin.store.end_session(sid)
        if self._who is not None:
            signin.store.drop_user(self._who.user_id)
        self._set_cookie(PANEL_COOKIE, "", 0, panel=True)
        self._set_cookie(SESSION_COOKIE, "", 0)
        self._send(200, b'{"ok": true}', "application/json")

    # -- /print ------------------------------------------------------------

    def _get_print(self, query: str, user: str) -> None:
        self._require_navigation()
        cfg = self.svc.cfg
        if cfg.bambuddy is None:
            raise ConfigError("BamBuddy isn't configured on the server")
        req = parse_print_query(query)
        if req.part_id is None:
            raise BadRequest("No part selected", "Right-click a part in the parts list")
        if self.svc.signin is not None and self._who is None:
            start = "/auth/start?" + urlencode({"next": f"/print?{query}"[:1500]})
            self._page(401, "Sign in", _sign_in_page(start))
            return
        with self._onshape() as onshape, self.svc.bambuddy() as bb:
            doc = onshape.get_document_name(req.document_id)
            part = onshape.get_part_name(req)
            views = self._printer_views(bb)
        token = self.svc.tokens.issue(user, _bind(req))
        body = _print_form(req, doc, part, views, cfg, token)
        self._page(200, f"Print {part}", body)

    def _post_print(self, user: str, panel: bool) -> None:
        self._require_same_origin_post()
        form = self._read_form()
        req, face, orientation, printer, slot, extra = _selection(form, panel)
        fields = {k: form[k] for k in REQUEST_FIELDS if k in form and form[k] != ""}
        settings = _form_settings(form, self.svc.cfg)
        plate = check_bed_type(form.get("plate"))
        # Redeem last, so a typo in a setting doesn't burn the form. The panel's token
        # is bound to the Part Studio (the part comes from the live selection).
        bound = _bind_element(req) if panel else _bind(req)
        if not self.svc.tokens.redeem(form.get("csrf", ""), user, bound):
            raise Forbidden("This form has expired or was already used", "Reload it from Onshape")
        cfg = self.svc.cfg

        uid = self._uid()

        def work(progress: Callable[[str], None]) -> list[str]:
            with self.svc.onshape_for(uid) as onshape, self.svc.bambuddy() as bb:
                job_req = req
                if job_req.part_id is None:
                    job_req = replace(req, part_id=onshape.part_of_face(req, face))
                plan = printing.plan_print(
                    job_req, cfg, onshape, bb, printer, orientation, settings, slot, plate, extra
                )
                progress("Checked the part and the printer")
                out = printing.execute_print(plan, cfg, onshape, bb, queue=True, progress=progress)
            s = out.slice
            lines = plan.summary_lines()
            minutes = f"{s.print_time_seconds // 60} min" if s.print_time_seconds else "?"
            lines.append(f"Sliced:      {minutes}, {s.filament_used_g or 0:.1f} g")
            item = out.queue_item or {}
            lines.append(f"Queued:      item {item.get('id')} on {plan.printer.name}")
            return lines

        back = "/panel?" + urlencode({k: v for k, v in fields.items() if k != "p"}) if panel else ""
        job = self.svc.jobs.start(user, f"Part on printer {printer}", work, back=back)
        log.warning("print job %s started by %s for %s", job.id, user, req)
        self.send_response(303)
        self.send_header("Location", f"/jobs/{job.id}")
        self._headers(0)

    # -- /panel (Element right panel iframe, D-15) ----------------------------

    def _get_panel(self, query: str, user: str) -> None:
        cfg = self.svc.cfg
        self._frame_origin = cfg.onshape_base_url
        if self.headers.get("Sec-Fetch-Dest") != "iframe":
            raise Forbidden("This page only works inside Onshape's right panel")
        if cfg.bambuddy is None:
            raise ConfigError("BamBuddy isn't configured on the server")
        params = dict(parse_qsl(query, keep_blank_values=True))
        if params.get("server", cfg.onshape_base_url) != cfg.onshape_base_url:
            raise Forbidden("This panel only works with the configured Onshape server")
        req = parse_print_query(query)
        if self.svc.signin is not None and self._who is None:
            self._scripts = True
            self._page(200, "Sign in", _sign_in_panel())
            return
        with self._onshape() as onshape, self.svc.bambuddy() as bb:
            parts = {str(p.get("partId")): str(p.get("name")) for p in onshape.list_parts(req)}
            views = self._printer_views(bb)
        token = self.svc.tokens.issue(user, _bind_element(req))
        self._scripts = True
        ui = _bambuddy_ui(cfg, self.headers.get("Host", ""))
        body = _panel_body(req, parts, views, cfg, token, ui)
        if self._who is not None:
            body = (_who_line(self._who.name) + body
                    + f'<script src="/static/auth.js?v={static_version()}"></script>')  # fmt: skip
        self._page(200, "Print", body)

    def _printer_views(self, bb: BambuddyClient) -> list[PrinterView]:
        """Active printers with their state, loaded slots and the preset each slot maps to."""
        cfg = self.svc.cfg
        presets = cfg.bambuddy.presets if cfg.bambuddy else {}
        names = self.svc.preset_names(bb)
        views = []
        for p in bb.list_printers():
            if not p.is_active:
                continue
            status = bb.printer_status(p.id)
            configured = presets.get(p.model)
            suffix = preset_suffix(configured.filament) if configured else ""
            dual = p.nozzle_count > 1
            slots = [(s, match_preset(s, suffix, names)) for s in slots_from_status(status, dual)]
            views.append(PrinterView(p, _state(status), configured, slots))
        return views

    # -- Open in Bambu Studio ------------------------------------------------

    def _post_model_link(self) -> None:
        """A 15-minute download URL for the current selection as a 3MF (read-only)."""
        self._require_same_origin_post()
        form = self._read_form()
        # The link is fetched by Bambu Studio without cookies: it carries the user (D-23).
        selection = (_selection(form, panel=True), *self._studio_settings(form), self._uid())
        token = secrets.token_urlsafe(16)
        with self.svc.model_lock:
            links = self.svc.model_links
            links[token] = (time.monotonic() + MODEL_LINK_TTL, selection, None)
            while len(links) > 200:
                links.popitem(last=False)
        scheme = "https" if self.svc.cfg.server.tls_cert else "http"
        url = f"{scheme}://{self.headers.get('Host')}/models/{token}/os2slice.3mf"
        self._send(200, json.dumps({"url": url}).encode(), "application/json")

    # -- the shared web Bambu Studio session (D-21) ---------------------------

    def _studio_settings(self, form: dict[str, str]) -> tuple[PrintSettings, str | None]:
        return _form_settings(form, self.svc.cfg), check_bed_type(form.get("plate"))

    def _studio_project(
        self, selection: Selection, settings: PrintSettings, plate: str | None, uid: str | None
    ) -> tuple[bytes, printing.PrintPlan]:
        """The selection as a Bambu Studio project, sliced (not queued) for its settings."""
        req, face, orientation, printer, slot, extra = selection
        cfg = self.svc.cfg
        with self.svc.onshape_for(uid) as onshape, self.svc.bambuddy() as bb:
            if req.part_id is None:
                req = replace(req, part_id=onshape.part_of_face(req, face))
            plan = printing.plan_print(
                req, cfg, onshape, bb, printer, orientation, settings, slot, plate, extra
            )
            return printing.studio_project(plan, cfg, onshape, bb), plan

    def _web_studio(self) -> WebStudioConfig:
        ws = self.svc.cfg.web_studio
        if ws is None:
            raise ConfigError("The web Bambu Studio isn't set up", "Set web_studio_url (add-on)")
        return ws

    def _get_web_studio_status(self) -> None:
        if self.headers.get("Sec-Fetch-Site") != "same-origin":
            raise Forbidden("Only for the print panel")
        ws = self._web_studio()
        self._send(200, json.dumps(web_studio_status(ws)).encode(), "application/json")

    def _post_web_studio(self) -> None:
        """Hand the selection's 3MF to the shared session; the tab opens client-side."""
        self._require_same_origin_post()
        ws = self._web_studio()
        form = self._read_form()
        selection = _selection(form, panel=True)
        data, plan = self._studio_project(selection, *self._studio_settings(form), self._uid())
        stamp = time.strftime("%Y%m%d-%H%M%S")
        name = f"{files.sanitize(plan.part_name, 'part')}-{stamp}-{secrets.token_hex(2)}.3mf"
        ws.inbox.mkdir(parents=True, exist_ok=True)
        tmp = ws.inbox / f".{name}.part"
        tmp.write_bytes(data)
        os.replace(tmp, ws.inbox / name)  # the watcher only picks up finished *.3mf
        log.info("handed %s to the web Bambu Studio", name)
        self._send(200, json.dumps({"url": ws.url, "file": name}).encode(), "application/json")

    def _get_model(self, token: str) -> None:
        """Serve the 3MF to Bambu Studio. The token is the credential (no browser headers)."""
        with self.svc.model_lock:
            entry = self.svc.model_links.get(token)
        if entry is None or entry[0] < time.monotonic():
            self._page(404, "Link expired", "<p>Open it again from the Onshape panel.</p>")
            return
        expires, selection, data = entry
        if data is None:
            data, _ = self._studio_project(*selection)
            with self.svc.model_lock:
                self.svc.model_links[token] = (expires, selection, data)
        self.send_response(200)
        self.send_header("Content-Type", "model/3mf")
        self.send_header("Content-Disposition", 'attachment; filename="os2slice.3mf"')
        self._headers(len(data))
        self.wfile.write(data)

    # -- /panel/preview and /static ------------------------------------------

    def _get_preview(self, query: str) -> None:
        """The part as STL in the chosen orientation. Read-only; same-origin fetch only."""
        if self.headers.get("Sec-Fetch-Site") != "same-origin":
            raise Forbidden("The preview is only for the print panel")
        try:
            pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True, max_num_fields=12)
        except ValueError as e:
            raise BadRequest("Malformed query string") from e
        params: dict[str, str] = {}
        for k, v in pairs:
            if k not in PREVIEW_FIELDS or k in params:
                raise BadRequest(f"Unexpected parameter {k[:30]!r}")
            params[k] = v
        fields = {k: v for k, v in params.items() if k in REQUEST_FIELDS and v != ""}
        req = parse_print_query(urlencode(fields))
        face = params.get("face", "")
        if face and not FACE_ID_RE.fullmatch(face):
            raise BadRequest("Invalid face ID")
        orient = params.get("orient", "as-modeled")
        orientation = (
            Orientation("face", face)
            if orient == "face" and face
            else Orientation.parse("as-modeled" if orient == "face" else orient)
        )
        if req.part_id is None and not face:
            raise BadRequest("Select a part")
        extra = [p for p in params.get("extra", "").split(",") if p]
        if len(extra) >= MAX_PARTS or not all(PART_ID_RE.fullmatch(p) for p in extra):
            raise BadRequest("Invalid part list")
        only = params.get("only", "")
        # Per user: a cached preview must never reach someone who can't open the part.
        key = (f"{self._uid() or ''}|{_bind(req)}|{','.join(extra)}|{face}|"
               f"{orientation.kind}:{orientation.value}")  # fmt: skip
        if not extra:
            stl = self.svc.previews.get(key)
            if stl is None:
                with self._onshape() as onshape:
                    if req.part_id is None:
                        req = replace(req, part_id=onshape.part_of_face(req, face))
                    stl = printing.oriented_stl(orientation, onshape, req)
                self.svc.previews.put(key, stl)
        else:
            ids = [req.part_id or "", *extra]
            if only not in ids:
                raise BadRequest("Unknown part for the preview")
            stl = self.svc.previews.get(f"{key}|{only}")
            if stl is None:
                with self._onshape() as onshape:
                    placed = printing.oriented_parts(orientation, onshape, req, ids)
                for part_id, data in zip(ids, placed, strict=True):
                    self.svc.previews.put(f"{key}|{part_id}", data)
                stl = placed[ids.index(only)]
        self._cache = "private, max-age=300"
        self._send(200, stl, "model/stl")

    def _get_static(self, name: str) -> None:
        if name not in STATIC_FILES:
            self._page(404, "Not found", "<p>Nothing here.</p>")
            return
        body = resources.files("os2slice").joinpath("static", *name.split("/")).read_bytes()
        self._cache = "public, max-age=86400"
        self._send(200, body, "text/javascript; charset=utf-8")

    def _read_form(self) -> dict[str, str]:
        ctype = self.headers.get("Content-Type", "").split(";")[0].strip()
        if ctype != "application/x-www-form-urlencoded":
            raise BadRequest("Expected a form submission")
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError as e:
            raise BadRequest("Missing Content-Length") from e
        if not 0 < length <= MAX_BODY:
            raise BadRequest("Form too large")
        raw = self.rfile.read(length).decode("utf-8", errors="strict")
        try:
            pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True, max_num_fields=40)
        except ValueError as e:
            raise BadRequest("Malformed form") from e
        form: dict[str, str] = {}
        for k, v in pairs:
            if k not in FORM_FIELDS or k in form:
                raise BadRequest(f"Unexpected form field {k[:30]!r}")
            form[k] = v
        return form

    # -- /jobs ---------------------------------------------------------------

    def _get_job(self, job_id: str, user: str) -> None:
        job = self.svc.jobs.get(job_id, user)
        if job is None:
            self._page(404, "Job not found", "<p>No such job (jobs are kept in memory).</p>")
            return
        if self.headers.get("Sec-Fetch-Dest") == "iframe" and job.back:
            self._frame_origin = self.svc.cfg.onshape_base_url
        ui = _bambuddy_ui(self.svc.cfg, self.headers.get("Host", ""))
        self._page(200, "Print job", _job_body(job, ui), refresh=not job.finished)

    # -- responses -----------------------------------------------------------

    def _error_page(self, e: Os2sliceError) -> None:
        fix = f"<p>{_e(e.fix)}</p>" if e.fix else ""
        self._page(e.http_status, "Can't do that", f"<p><b>{_e(e.message)}</b></p>{fix}")

    def _page(self, status: int, title: str, body: str, refresh: bool = False) -> None:
        doc = _layout(title, body, refresh).encode()
        self._send(status, doc, "text/html; charset=utf-8")

    def _send(self, status: int, body: bytes, ctype: str = "text/plain") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self._headers(len(body))
        self.wfile.write(body)

    def _headers(self, length: int) -> None:
        frame = getattr(self, "_frame_origin", "") or "'none'"
        script = (
            "; script-src 'self'; connect-src 'self'" if getattr(self, "_scripts", False) else ""
        )
        self.send_header("Content-Security-Policy", f"{CSP}{script}; frame-ancestors {frame}")
        cache = getattr(self, "_cache", "")
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, cache if (k == "Cache-Control" and cache) else v)
        for cookie in getattr(self, "_cookies", []):
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", str(length))
        self.end_headers()

    def _method_not_allowed(self) -> None:
        self.send_response(405)
        self.send_header("Allow", "GET, POST")
        self._headers(0)

    def log_message(self, format: str, *args: Any) -> None:
        log.debug("%s %s", self.address_string(), format % args)


# -- rendering -------------------------------------------------------------------


def _e(text: object) -> str:
    return html.escape(str(text), quote=True)


def _bind(req: ExportRequest) -> str:
    return "|".join((req.document_id, req.wvm, req.wvm_id, req.element_id, req.part_id or "",
                     req.configuration))  # fmt: skip


class PreviewCache:
    """A few recent oriented STLs, so flipping options doesn't re-export from Onshape."""

    def __init__(self, entries: int = 16, max_bytes: int = 64_000_000) -> None:
        self._items: OrderedDict[str, bytes] = OrderedDict()
        self._lock = threading.Lock()
        self._entries, self._max_bytes = entries, max_bytes

    def get(self, key: str) -> bytes | None:
        with self._lock:
            if key in self._items:
                self._items.move_to_end(key)
                return self._items[key]
        return None

    def put(self, key: str, value: bytes) -> None:
        with self._lock:
            self._items[key] = value
            self._items.move_to_end(key)
            while len(self._items) > self._entries or (
                sum(map(len, self._items.values())) > self._max_bytes and len(self._items) > 1
            ):
                self._items.popitem(last=False)


def _bind_element(req: ExportRequest) -> str:
    parts = (req.document_id, req.wvm, req.wvm_id, req.element_id, req.configuration)
    return "panel|" + "|".join(parts)


def _state(status: dict[str, Any]) -> str:
    if not status.get("connected", True):
        return "offline"
    state = str(status.get("state") or "unknown").lower()
    if status.get("awaiting_plate_clear"):
        state += ", plate not cleared"
    return state


def _options(choices: Iterable[tuple[str, str]], selected: str) -> str:
    return "".join(
        f'<option value="{_e(v)}"{" selected" if v == selected else ""}>{_e(label)}</option>'
        for v, label in choices
    )


def _print_form(
    req: ExportRequest,
    doc: str,
    part: str,
    views: list[PrinterView],
    cfg: Config,
    token: str,
) -> str:
    assert cfg.bambuddy is not None  # noqa: S101 - checked by the caller
    hidden = "".join(
        f'<input type="hidden" name="{k}" value="{_e(v)}">'
        for k, v in (
            ("d", req.document_id),
            ("wv", req.wvm),
            ("wvid", req.wvm_id),
            ("e", req.element_id),
            ("p", req.part_id or ""),
            ("c", req.configuration),
            ("csrf", token),
        )
    )
    config = (
        f"<dt>Configuration</dt><dd><code>{_e(req.configuration)}</code></dd>"
        if req.configuration
        else ""
    )
    start = (
        "waits in BamBuddy's queue until you press Start"
        if cfg.bambuddy.manual_start
        else "starts as soon as the printer is free"
    )
    return f"""
<dl><dt>Part</dt><dd>{_e(part)}</dd><dt>Document</dt><dd>{_e(doc)}</dd>{config}</dl>
<form method="post" action="/print">{hidden}
{_printer_select(views, cfg)}
{_plate_select(cfg)}
<label>Orientation <select name="orient">{_options(ORIENT_CHOICES, "as-modeled")}</select></label>
{_settings_fields(cfg)}
<p class="muted">The print {_e(start)}.</p>
<button type="submit">Slice and queue print</button>
</form>"""


def _plate_select(cfg: Config) -> str:
    label = PLATE_LABELS.get(cfg.default_bed_type or "", cfg.default_bed_type)
    default = f"Default: {label}" if cfg.default_bed_type else "Printer default"
    opts = _options([("", default), *PLATE_LABELS.items()], "")
    return f'<label>Build plate <select name="plate">{opts}</select></label>'


def _form_settings(form: dict[str, str], cfg: Config) -> PrintSettings:
    """Settings from one of our own forms, where a missing checkbox means unchecked."""
    return PrintSettings.from_strings({c: "" for c in CHECKBOXES} | form, cfg.print_defaults)


def _settings_fields(cfg: Config) -> str:
    d = cfg.print_defaults

    def checked(on: bool) -> str:
        return " checked" if on else ""

    supports = _options(((s, s.capitalize()) for s in SUPPORTS), d.supports)
    lo, hi = SHELL_RANGE
    return f"""<div class="row">
<label>Walls <input type="number" name="walls" min="1" max="10" value="{d.walls}" required></label>
<label>Infill % <input type="number" name="infill" min="0" max="100" value="{d.infill}" required>
</label><label>Supports <select name="supports">{supports}</select></label>
</div>
<label class="check"><input type="checkbox" name="build_plate_only" value="true"{checked(d.build_plate_only)}>
Supports from the build plate only</label>
<div class="row">
<label>Top layers <input type="number" name="top_layers" min="{lo}" max="{hi}" value="{d.top_layers}" required></label>
<label>Bottom layers <input type="number" name="bottom_layers" min="{lo}" max="{hi}" value="{d.bottom_layers}" required></label>
<label>Copies <input type="number" name="copies" min="{COPIES_RANGE[0]}" max="{COPIES_RANGE[1]}" value="{d.copies}" required></label>
</div>
<label class="check"><input type="checkbox" name="brim" value="true"{checked(d.brim)}> Brim</label>"""  # noqa: E501


@dataclass(frozen=True)
class PrinterView:
    printer: Printer
    state: str
    presets: PresetChoice | None  # configured for this model
    slots: list[tuple[Slot, str | None]]  # loaded slot → matching filament preset


Selection = tuple[ExportRequest, str, Orientation, str, int | None, list[tuple[str, int]]]


def _selection(form: dict[str, str], panel: bool) -> Selection:
    """Validate what the page or panel selected: part(s), face, orientation, printer, slots."""
    fields = {k: form[k] for k in REQUEST_FIELDS if k in form and form[k] != ""}
    req = parse_print_query(urlencode(fields))
    face = form.get("face", "")
    if face and not FACE_ID_RE.fullmatch(face):
        raise BadRequest("Invalid face ID")
    orient = form.get("orient", "as-modeled")
    if orient == "face":
        if not face:
            raise BadRequest("Select the face to put on the bed")
        orientation = Orientation("face", face)
    else:
        orientation = Orientation.parse(orient)
    if not panel and req.part_id is None:
        raise BadRequest("No part selected", "Right-click a part in the parts list")
    if panel and req.part_id is None and not face:
        raise BadRequest("Select a part, or the face it should stand on")
    printer, slot = _printer_choice(form.get("printer", ""), form.get("filament", ""))
    extra = _parse_extra(form.get("extra", "")) if panel else []
    if extra and req.part_id is None:
        raise BadRequest("Select the parts to print")
    return req, face, orientation, printer, slot, extra


def web_studio_status(ws: WebStudioConfig, now: float | None = None) -> dict[str, Any]:
    """{"state": "free" | "busy" | "unknown", "viewers": n} from the add-on's status file."""
    now = time.time() if now is None else now
    try:
        data = json.loads(ws.status.read_text())
        viewers, updated = int(data["viewers"]), float(data["updated"])
    except (OSError, ValueError, KeyError, TypeError):
        return {"state": "unknown", "viewers": None}
    if now - updated > 15:  # the add-on writes it every 2 s
        return {"state": "unknown", "viewers": None}
    if data.get("app_running") is False:  # desktop up, Bambu Studio not (yet) running
        return {"state": "unknown", "viewers": viewers}
    return {"state": "busy" if viewers > 0 else "free", "viewers": viewers}


def _bambuddy_ui(cfg: Config, host: str) -> str:
    """Where browsers reach BamBuddy's own UI (the add-on's base_url is internal)."""
    if cfg.bambuddy and cfg.bambuddy.public_url:
        return cfg.bambuddy.public_url
    return f"http://{host.rsplit(':', 1)[0]}:8000"


def _parse_extra(value: str) -> list[tuple[str, int]]:
    """'JKD:4,JLD:0' → [('JKD', 4), ('JLD', 0)]: further parts and their tray ids."""
    if not value:
        return []
    items = value.split(",")
    if len(items) >= MAX_PARTS:
        raise BadRequest(f"At most {MAX_PARTS} parts per print")
    out = []
    for item in items:
        m = EXTRA_RE.fullmatch(item)
        if not m:
            raise BadRequest("Invalid part/filament list")
        out.append((m.group(1), int(m.group(2))))
    return out


def _split_printer(value: str) -> tuple[str, int | None]:
    """'X1C_01|2' → ('X1C_01', 2): a printer and a global tray id; 'X1C_01' → preset filament."""
    m = re.fullmatch(r"(.+)\|(\d{1,3})", value)
    return (m.group(1), int(m.group(2))) if m else (value, None)


def _printer_choice(printer: str, filament: str) -> tuple[str, int | None]:
    """The page's combined 'printer|tray' menu, or the panel's printer + filament menus."""
    name, slot = _split_printer(printer)
    if filament:
        if slot is not None or not re.fullmatch(r"\d{1,3}", filament):
            raise BadRequest("Invalid filament choice")
        slot = int(filament)
    return name, slot


def _slot_usable(slot: Slot, dual: bool) -> bool:
    """On dual-nozzle printers a slot is usable once we know which nozzle it feeds."""
    return not dual or slot.extruder in NOZZLE_NAMES


def _default_choice(view: PrinterView) -> str:
    """Prefer a loaded slot with the configured material, then a lone slot, then the preset."""
    name = view.printer.name
    dual = view.printer.nozzle_count > 1
    usable = [(s, p) for s, p in view.slots if p is not None and _slot_usable(s, dual)]
    if not usable or view.presets is None:
        return name
    configured = view.presets.filament.upper()
    for s, _ in usable:
        if re.search(rf"\b{re.escape(s.material.upper())}\b", configured):
            return f"{name}|{s.tray_id}"
    return f"{name}|{usable[0][0].tray_id}" if len(usable) == 1 else name


def _printer_select(views: list[PrinterView], cfg: Config) -> str:
    default_printer = cfg.bambuddy.default_printer if cfg.bambuddy else ""
    selected = next(
        (_default_choice(v) for v in views if v.printer.name == default_printer and v.presets),
        "",
    )
    groups, skipped = [], []
    for v in views:
        if v.presets is None:
            skipped.append(v.printer.name)
            continue
        name = v.printer.name
        preset_label = v.presets.filament.split(" @")[0]
        opts = [(name, f"{name} · preset filament ({preset_label})", "", False)]
        for slot, preset in v.slots:
            if not _slot_usable(slot, v.printer.nozzle_count > 1):
                label, disabled = f"{slot.describe()} (nozzle unknown)", True
            elif preset is None:
                label, disabled = f"{slot.describe()} (no matching preset)", True
            else:
                label, disabled = slot.describe(), False
            opts.append((f"{name}|{slot.tray_id}", f"{name} · {label}", slot.color, disabled))
        html_opts = "".join(
            f'<option value="{_e(val)}"'
            + (f' data-color="{_e(color)}"' if color else "")
            + (" disabled" if disabled else "")
            + (" selected" if val == selected and not disabled else "")
            + f">{_e(label)}</option>"
            for val, label, color, disabled in opts
        )
        groups.append(
            f'<optgroup label="{_e(f"{name} ({v.printer.model}), {v.state}")}">'
            f"{html_opts}</optgroup>"
        )
    note = (
        f'<p class="muted">No presets configured for: {_e(", ".join(skipped))}</p>'
        if skipped
        else ""
    )
    return (
        f'<label>Printer and filament <select name="printer" required>{"".join(groups)}'
        f"</select></label>{note}"
    )


def _filament_choices(v: PrinterView) -> list[dict[str, Any]]:
    """The panel's filament menu for one printer: the preset filament, then each slot."""
    default = _split_printer(_default_choice(v))[1]
    preset_label = v.presets.filament.split(" @")[0] if v.presets else ""
    out: list[dict[str, Any]] = [
        {"value": "", "label": f"Preset filament ({preset_label})", "default": default is None}
    ]
    for slot, preset in v.slots:
        if not _slot_usable(slot, v.printer.nozzle_count > 1):
            label, disabled = f"{slot.describe()} (nozzle unknown)", True
        elif preset is None:
            label, disabled = f"{slot.describe()} (no matching preset)", True
        else:
            label, disabled = slot.describe(), False
        out.append(
            {
                "value": str(slot.tray_id),
                "label": label,
                "color": slot.color,
                "disabled": disabled,
                "default": slot.tray_id == default and not disabled,
            }
        )
    return out


def _panel_printer_selects(views: list[PrinterView], cfg: Config) -> str:
    """Separate printer and filament menus; panel.js refills the filament menu per printer."""
    usable = [v for v in views if v.presets is not None]
    skipped = [v.printer.name for v in views if v.presets is None]
    wanted = cfg.bambuddy.default_printer if cfg.bambuddy else ""
    first = next((v for v in usable if v.printer.name == wanted), usable[0] if usable else None)
    printers = "".join(
        f'<option value="{_e(v.printer.name)}"{" selected" if v is first else ""}>'
        f"{_e(f'{v.printer.name} ({v.printer.model}), {v.state}')}</option>"
        for v in usable
    )
    choices = {v.printer.name: _filament_choices(v) for v in usable}
    # Server-rendered options for the first printer, so the form is complete before JS runs.
    filaments = "".join(
        f'<option value="{_e(c["value"])}"'
        + (f' data-color="{_e(c["color"])}"' if c.get("color") else "")
        + (" disabled" if c.get("disabled") else "")
        + (" selected" if c["default"] else "")
        + f">{_e(c['label'])}</option>"
        for c in (choices[first.printer.name] if first else [])
    )
    note = (
        f'<p class="muted">No presets configured for: {_e(", ".join(skipped))}</p>'
        if skipped
        else ""
    )
    return (
        f'<label>Printer <select name="printer" required>{printers}</select></label>'
        f'<label>Filament <select name="filament" data-choices="{_e(json.dumps(choices))}">'
        f"{filaments}</select></label>{note}"
    )


@functools.cache
def static_version() -> str:
    """A hash of the static files, so a deploy busts browsers' day-long cache of them."""
    h = hashlib.sha256()
    for name in sorted(STATIC_FILES):
        h.update(resources.files("os2slice").joinpath("static", *name.split("/")).read_bytes())
    return h.hexdigest()[:12]


def _panel_body(
    req: ExportRequest,
    parts: dict[str, str],
    views: list[PrinterView],
    cfg: Config,
    token: str,
    bambuddy_ui: str = "",
) -> str:
    ids = {"documentId": req.document_id, "workspaceId": req.wvm_id, "elementId": req.element_id}
    hidden = "".join(
        f'<input type="hidden" name="{k}" value="{_e(v)}">'
        for k, v in (
            ("d", req.document_id),
            ("wv", req.wvm),
            ("wvid", req.wvm_id),
            ("e", req.element_id),
            ("c", req.configuration),
            ("p", ""),
            ("face", ""),
            ("extra", ""),
            ("csrf", token),
        )
    )
    orient = _options([("face", "Selected face down"), *ORIENT_CHOICES], "as-modeled")
    manual = cfg.bambuddy is not None and cfg.bambuddy.manual_start
    start = "Waits in BamBuddy until you press Start." if manual else ""
    beds = {v.printer.name: BED_MM.get(v.printer.model, (256, 256)) for v in views}
    # The preview lays copies out like printing.copy_offsets, with the same spacing.
    layout = {"gap": printing.COPY_GAP, "brimGap": printing.BRIM_GAP, "margin": printing.BED_MARGIN}
    return f"""<div id="panel" data-onshape="{_e(cfg.onshape_base_url)}"
 data-ids="{_e(json.dumps(ids))}" data-parts="{_e(json.dumps(parts))}"
 data-beds="{_e(json.dumps(beds))}" data-layout="{_e(json.dumps(layout))}">
<p id="selection" class="sel">Loading…</p>
{_studio_links(cfg)}
<p class="links"><a href="{_e(bambuddy_ui)}/queue" target="_blank" rel="noopener">
Open BamBuddy</a></p>
<div id="preview" class="preview">
<p id="preview-note" class="muted">Select a part to preview it.</p></div>
<form id="printform" method="post" action="/panel/print">{hidden}
{_panel_printer_selects(views, cfg)}
<div id="extras"></div>
{_plate_select(cfg)}
<label>Orientation <select name="orient">{orient}</select></label>
{_settings_fields(cfg)}
<p class="muted">{_e(start)}</p>
<button type="submit" disabled>Slice and queue print</button>
</form></div>
<script src="/static/panel.js?v={static_version()}"></script>
<script type="module" src="/static/preview.js?v={static_version()}"></script>"""


def _sign_in_panel() -> str:
    return f"""<div id="signin">
<p>Sign in with your Onshape account to print from this panel. os2slice then reads parts
with your own access (read-only).</p>
<p><button type="button" id="signin-button">Sign in with Onshape</button></p>
<p id="signin-note" class="muted">A small Onshape window opens; allow pop-ups if asked.</p>
</div>
<script src="/static/auth.js?v={static_version()}"></script>"""


def _sign_in_page(start: str) -> str:
    return (
        "<p>os2slice reads parts with your own Onshape access. Sign in once, then you come "
        f'back here.</p><p><a href="{_e(start)}">Sign in with Onshape</a></p>'
    )


def _signed_in_page(name: str, claim: str, next_path: str) -> str:
    return f"""<div id="signed-in" data-claim="{_e(claim)}" data-next="{_e(next_path)}">
<p>Signed in to os2slice as <b>{_e(name)}</b>.</p>
<p class="muted">This window closes by itself. If it doesn't, close it and go back to Onshape.</p>
</div>
<script src="/static/auth.js?v={static_version()}"></script>"""


def _who_line(name: str) -> str:
    return (
        f'<p class="muted links">Signed in as {_e(name)} · '
        '<a href="#" id="signout-button">Sign out</a></p>'
    )


def _studio_links(cfg: Config) -> str:
    local = '<a id="studio-link" class="off" href="#">on this computer</a>'
    if cfg.web_studio is None:
        return f'<p class="links">Open in Bambu Studio: {local}</p>'
    web = (
        f'<a id="web-studio-link" class="off" href="{_e(cfg.web_studio.url)}" target="_blank" '
        'rel="noopener">in the browser</a> <span id="web-studio-state" class="muted"></span>'
    )
    return f'<p class="links">Open in Bambu Studio: {web} · {local}</p>'


def _job_body(job: Job, bambuddy_ui: str = "") -> str:
    steps = "".join(f"<li>{_e(s)}</li>" for s in job.steps) or "<li>Waiting to start…</li>"
    if job.state == "done":
        tail = "<h2>Queued ✓</h2><pre>" + _e("\n".join(job.result)) + "</pre>"
    elif job.state == "failed":
        fix = f"<p>{_e(job.fix)}</p>" if job.fix else ""
        tail = f"<h2>Failed</h2><p><b>{_e(job.error)}</b></p>{fix}"
    else:
        tail = '<p class="muted">Working… this page refreshes itself.</p>'
    links = []
    if job.back and job.finished:
        links.append(f'<a href="{_e(job.back)}">Print another</a>')
    if bambuddy_ui:
        links.append(
            f'<a href="{_e(bambuddy_ui)}/queue" target="_blank" rel="noopener">'
            "Open BamBuddy's queue</a>"
        )
    back = f'<p class="links">{" · ".join(links)}</p>' if links else ""
    return f"<ol>{steps}</ol>{tail}{back}"


def _layout(title: str, body: str, refresh: bool) -> str:
    meta = '<meta http-equiv="refresh" content="2">' if refresh else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">{meta}
<title>{_e(title)} · os2slice</title>
<style>
:root {{ --bg:#fff; --fg:#1b1d21; --muted:#5f6670; --line:#d5d9df; --accent:#0b6bcb; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#16181c; --fg:#e6e8eb; --muted:#9aa3ad; --line:#343a42; --accent:#5aa9ff; }}
}}
body {{ background:var(--bg); color:var(--fg); font:15px/1.45 system-ui,sans-serif;
       max-width:34rem; margin:0 auto; padding:16px; }}
h1 {{ font-size:1.25rem; margin:.2rem 0 1rem; }} h2 {{ font-size:1.05rem; }}
dl {{ display:grid; grid-template-columns:auto 1fr; gap:.2rem .8rem; }} dt {{ color:var(--muted); }}
dd {{ margin:0; overflow-wrap:anywhere; }}
label {{ display:block; margin:.7rem 0 .2rem; }} label.check {{ display:flex; gap:.4rem; }}
select,input[type=number] {{ display:block; width:100%; box-sizing:border-box;
  margin-top:.2rem; padding:.4rem; background:var(--bg); color:var(--fg);
  border:1px solid var(--line); border-radius:6px; }}
.row {{ display:grid; grid-template-columns:repeat(3,1fr); gap:.6rem; }}
button {{ margin-top:1rem; width:100%; padding:.65rem; font-size:1rem; border:0; border-radius:6px;
  background:var(--accent); color:#fff; cursor:pointer; }}
.muted {{ color:var(--muted); font-size:.9rem; }} pre {{ white-space:pre-wrap; }}
.sel {{ padding:.5rem; border:1px solid var(--line); border-radius:6px; }}
.links {{ margin:.4rem 0; font-size:.9rem; }} a.off {{ opacity:.45; pointer-events:none; }}
.preview {{ position:relative; height:230px; margin-top:.6rem; border:1px solid var(--line);
  border-radius:6px; overflow:hidden; }}
.preview canvas {{ display:block; width:100%; height:100%; touch-action:none; }}
#preview-note {{ position:absolute; left:.5rem; bottom:.3rem; margin:0; pointer-events:none; }}
button:disabled {{ opacity:.45; cursor:default; }} a {{ color:var(--accent); }}
</style></head><body><h1>{_e(title)}</h1>{body}</body></html>"""
