"""The config page /admin (D-28): access rules, forms, saving and reloading."""

from __future__ import annotations

import re
import sys
import threading
from collections.abc import Callable, Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from os2slice import admin, adminauth, cli, config, server
from os2slice.adminauth import AdminStore
from os2slice.auth import Keys
from os2slice.logsetup import log_path
from os2slice.modules import registry
from os2slice.modules.base import Field, Health, ModuleSpec, ProfileCatalog
from os2slice.modules.moonraker import Moonraker
from os2slice.onshape import OnshapeClient
from tests.fakes import FakeBambuddy, fake_onshape
from tests.fakes_printers import FakeMoonraker

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

HOST = "localhost:8765"
SAME = {"Host": HOST, "Sec-Fetch-Site": "same-origin", "Origin": f"http://{HOST}"}
PASSWORD = "correct horse battery"
SECRET = "S3CRET-value-do-not-show"

MODERN = """
[export]
dir = "{exports}"

[targets.farm]
kind = "bambuddy"
url = "http://bambuddy.test:8000"

[targets.farm.models."A1 Mini".profiles]
printer = "Bambu Lab A1 mini 0.4 nozzle"
process = "0.20mm Standard @BBL A1M"
filament = "Bambu PLA Basic @BBL A1M"
"""

LEGACY = """
[export]
dir = "{exports}"

[bambuddy]
base_url = "http://bambuddy.test:8000/"
default_printer = "A1 Mini"
public_url = "https://print.example.duckdns.org:8000"

[bambuddy.presets."A1 Mini"]
printer = "Bambu Lab A1 mini 0.4 nozzle"
process = "0.20mm Standard @BBL A1M"
filament = "Bambu PLA Basic @BBL A1M"
bed_type = "Smooth PEI Plate"

[bambuddy.presets.X1C]
printer = "Bambu Lab X1 Carbon 0.4 nozzle"
process = "0.20mm Standard @BBL X1C"
filament = "Bambu PLA Basic @BBL X1C"
source = "cloud"
"""

LAN = """
[server]
identity = "lan"
bind = "0.0.0.0"
port = 8765
hosts = ["localhost:8765"]
tls_cert = "/nonexistent/fullchain.pem"
tls_key = "/nonexistent/privkey.pem"
"""


class GcodeSlicer:
    """A test-only server-side slicer that makes plain G-code."""

    spec = ModuleSpec(
        kind="test-gcode",
        label="Test G-code slicer",
        role="slicer",
        technology="fdm",
        fields=(
            Field("url", "URL", "url", required=True),
            Field("token", "Token", "secret"),
        ),
        makes=("gcode",),
    )

    def __init__(self, values: Any, *, transport: Any = None) -> None:
        self.values = values

    def check(self) -> Health:
        return Health(True, f"slicer ok at {self.values['url']}")

    def profiles(self, printer_model: str = "") -> ProfileCatalog:
        return ProfileCatalog(("Voron profile",), ("0.2mm Voron",), ("Generic PLA",))

    def slice(self, job: Any, progress: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


@pytest.fixture(autouse=True)
def setup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adminauth, "SCRYPT_N", 2**10)
    monkeypatch.setitem(registry.SLICERS, "test-gcode", GcodeSlicer)
    monkeypatch.setitem(registry.TARGETS, "moonraker", Moonraker)


class Admin:
    def __init__(
        self,
        tmp: Path,
        text: str = MODERN,
        *,
        password: bool = True,
        secrets: Callable[[str], str | None] | None = None,
        transports: dict[str, httpx.BaseTransport] | None = None,
    ) -> None:
        self.path = tmp / "cfgdir" / "config.toml"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(text.format(exports=tmp / "exports"), encoding="utf-8")
        cfg = config.load(self.path, create=False)
        self.store = AdminStore(tmp / "admin.json")
        if password:
            self.store.set_password(PASSWORD)
        self.fake = FakeBambuddy()
        lookup = secrets or (lambda name: "k")
        transport = httpx.MockTransport(self.fake)
        modules = registry.Modules.from_config(
            cfg, secrets=lookup, transport=transport, transports=transports
        )
        self.svc = server.Service(
            cfg,
            onshape=lambda: OnshapeClient(
                "https://cad.onshape.com", Keys("a", "b"),
                transport=httpx.MockTransport(fake_onshape),
            ),
            modules=modules,
            admin_store=self.store,
            secrets=lookup,
            module_transport=transport,
            module_transports=transports,
        )  # fmt: skip
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(self.svc))
        threading.Thread(target=self.httpd.serve_forever, args=(0.01,), daemon=True).start()
        self.http = httpx.Client(
            base_url=f"http://127.0.0.1:{self.httpd.server_port}", follow_redirects=False
        )
        self.session = ""
        self.pages: list[str] = []

    def _cookie(self) -> dict[str, str]:
        return {"Cookie": f"{admin.COOKIE}={self.session}"} if self.session else {}

    def get(self, path: str, **headers: str) -> httpx.Response:
        self.http.cookies.clear()
        r = self.http.get(path, headers={"Host": HOST, **self._cookie(), **headers})
        self.pages.append(r.text)
        return r

    def post(self, path: str, data: dict[str, str], **headers: str) -> httpx.Response:
        self.http.cookies.clear()
        h = {**SAME, **self._cookie(), **headers}
        r = self.http.post(path, data=data, headers={k: v for k, v in h.items() if v})
        self.pages.append(r.text)
        return r

    def csrf(self, page: str, action: str) -> str:
        m = re.search(
            rf'action="{re.escape(action)}"[^>]*><input type="hidden" name="csrf" '
            r'value="([^"]+)"',
            self.get(page).text,
        )
        assert m, f"no form for {action} on {page}"
        return m.group(1)

    def submit(self, page: str, action: str, data: dict[str, str]) -> httpx.Response:
        return self.post(action, {"csrf": self.csrf(page, action), **data})

    def login(self, password: str = PASSWORD) -> httpx.Response:
        r = self.submit(admin.LOGIN, admin.LOGIN, {"password": password})
        if r.status_code == 303:
            m = re.search(rf"{admin.COOKIE}=([^;]+)", r.headers["set-cookie"])
            assert m
            self.session = m.group(1)
        return r

    def close(self) -> None:
        self.httpd.shutdown()


@pytest.fixture
def adm(tmp_path: Path) -> Iterator[Admin]:
    a = Admin(tmp_path)
    yield a
    a.close()


@pytest.fixture
def logged(adm: Admin) -> Admin:
    assert adm.login().status_code == 303
    return adm


# ---- access ----------------------------------------------------------------


def test_no_password_is_503_everywhere(tmp_path: Path) -> None:
    a = Admin(tmp_path, password=False)
    try:
        for path in ("/admin", "/admin/", "/admin/login", "/admin/server"):
            r = a.get(path)
            assert r.status_code == 503 and "os2slice admin-password" in r.text
        r = a.post("/admin/login", {"password": PASSWORD})
        assert r.status_code == 503
        assert "set-cookie" not in r.headers
    finally:
        a.close()


def test_login_and_wrong_password_backoff(adm: Admin) -> None:
    for _ in range(5):
        r = adm.login("wrong password here")
        assert r.status_code == 401 and "Wrong password" in r.text
    r = adm.login()  # the right one, but locked out now
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0
    assert not adm.session


def test_login_sets_strict_cookie_and_redirects(adm: Admin) -> None:
    r = adm.login()
    assert r.status_code == 303 and r.headers["location"] == "/admin"
    cookie = r.headers["set-cookie"]
    assert "Path=/admin" in cookie and "HttpOnly" in cookie and "SameSite=Strict" in cookie
    assert "Secure" not in cookie  # identity "none", plain http
    assert adm.get("/admin").status_code == 200
    assert adm.get("/admin/login").headers["location"] == "/admin"


def test_secure_cookie_under_lan_identity(tmp_path: Path) -> None:
    a = Admin(tmp_path, MODERN + LAN)
    try:
        r = a.login()
        assert r.status_code == 303 and "; Secure" in r.headers["set-cookie"]
    finally:
        a.close()


@pytest.mark.parametrize(
    "headers",
    [
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
        {"Sec-Fetch-Site": "", "Origin": ""},  # neither header
        {"Origin": "http://evil.example"},
        {"Origin": "null"},
    ],
)
def test_login_needs_same_origin(adm: Admin, headers: dict[str, str]) -> None:
    token = adm.csrf(admin.LOGIN, admin.LOGIN)
    r = adm.post(admin.LOGIN, {"csrf": token, "password": PASSWORD}, **headers)
    assert r.status_code == 403 and "set-cookie" not in r.headers


def test_login_without_fetch_metadata_but_same_origin(adm: Admin) -> None:
    token = adm.csrf(admin.LOGIN, admin.LOGIN)
    r = adm.post(admin.LOGIN, {"csrf": token, "password": PASSWORD}, **{"Sec-Fetch-Site": ""})
    assert r.status_code == 303


def test_routes_without_session_redirect_to_login(adm: Admin) -> None:
    for path in admin.GET_ROUTES:
        r = adm.get(path)
        assert r.status_code == 303 and r.headers["location"] == admin.LOGIN, path
    adm.session = "not-a-real-session"
    assert adm.get("/admin").headers["location"] == admin.LOGIN
    r = adm.post("/admin/defaults", {"walls": "4"})
    assert r.status_code == 303 and r.headers["location"] == admin.LOGIN


def test_host_mismatch_is_refused(logged: Admin) -> None:
    r = logged.get("/admin", Host="evil.example:8765")
    assert r.status_code == 403
    assert logged.get("/admin/nope").status_code == 404
    assert logged.post("/admin/jobs", {}).status_code == 405


def test_csrf_missing_reused_and_bound(logged: Admin) -> None:
    before = logged.path.read_bytes()
    assert logged.post("/admin/defaults", {"walls": "4"}).status_code == 403
    token = logged.csrf("/admin/defaults", "/admin/defaults")
    form = {"csrf": token, "walls": "4", "infill": "20", "supports": "off", "top_layers": "5",
            "bottom_layers": "3", "copies": "1", "bed_type": ""}  # fmt: skip
    # bound to its form: no use on another action
    assert logged.post("/admin/server", {"csrf": token, "port": "9000"}).status_code == 403
    token = logged.csrf("/admin/defaults", "/admin/defaults")
    form["csrf"] = token
    assert logged.post("/admin/defaults", form).status_code == 303
    assert logged.post("/admin/defaults", form).status_code == 403  # reused
    assert before != logged.path.read_bytes()
    assert logged.svc.cfg.print_defaults.walls == 4  # reloaded


def test_csrf_is_bound_to_the_session(logged: Admin) -> None:
    token = logged.csrf("/admin/defaults", "/admin/defaults")
    logged.session = ""
    logged.login()  # a second session
    r = logged.post("/admin/defaults", {"csrf": token, "walls": "4"})
    assert r.status_code == 403


@pytest.mark.parametrize(
    "headers",
    [
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
        {"Origin": "http://evil.example"},
        {"Sec-Fetch-Site": "", "Origin": ""},
    ],
)
def test_cross_site_posts_are_refused(logged: Admin, headers: dict[str, str]) -> None:
    before = logged.path.read_bytes()
    token = logged.csrf("/admin/server", "/admin/server")
    r = logged.post("/admin/server", {"csrf": token, "port": "9000"}, **headers)
    assert r.status_code == 403
    assert logged.path.read_bytes() == before


def test_logout_revokes_the_session(logged: Admin) -> None:
    r = logged.submit("/admin", "/admin/logout", {})
    assert r.status_code == 303 and "Max-Age=0" in r.headers["set-cookie"]
    assert logged.get("/admin").headers["location"] == admin.LOGIN


def test_change_password(logged: Admin) -> None:
    r = logged.submit(
        "/admin/password",
        "/admin/password",
        {"current": "nope nope nope", "new": "x" * 12, "repeat": "x" * 12},
    )
    assert r.status_code == 401
    r = logged.submit(
        "/admin/password",
        "/admin/password",
        {"current": PASSWORD, "new": "short", "repeat": "short"},
    )
    assert r.status_code == 400 and "at least 12" in r.text
    new = "a brand new passphrase"
    r = logged.submit("/admin/password", "/admin/password",
                      {"current": PASSWORD, "new": new, "repeat": new})  # fmt: skip
    assert r.status_code == 303
    assert logged.store.verify(new)
    m = re.search(rf"{admin.COOKIE}=([^;]+)", r.headers["set-cookie"])
    assert m
    logged.session = m.group(1)
    assert logged.get("/admin").status_code == 200


# ---- editing -----------------------------------------------------------------


def test_add_slicer_target_and_printer(tmp_path: Path) -> None:
    moon = FakeMoonraker()
    a = Admin(tmp_path, transports={"voron": httpx.MockTransport(moon)})
    try:
        a.login()
        old = a.svc.modules
        r = a.get("/admin/slicers/new?kind=test-gcode")
        assert r.status_code == 200 and "Test G-code slicer" in r.text
        r = a.submit("/admin/slicers/new?kind=test-gcode", "/admin/slicers/save",
                     {"kind": "test-gcode", "mode": "new", "key": "gc",
                      "f.url": "http://slicer.lan:3003/", "do": "save"})  # fmt: skip
        assert r.status_code == 303, r.text
        r = a.submit("/admin/targets/new?kind=moonraker", "/admin/targets/save",
                     {"kind": "moonraker", "mode": "new", "key": "voron",
                      "f.url": "http://voron.lan:7125", "f.folder": "os2slice",
                      "do": "save"})  # fmt: skip
        assert r.status_code == 303, r.text
        r = a.submit("/admin/printers/new", "/admin/printers/save",
                     {"mode": "new", "key": "x", "target": "voron", "slicer": "gc",
                      "model": "Voron 2.4", "bed_w": "350", "bed_d": "350.5",
                      "p.printer": "Voron profile", "p.process": "0.2mm Voron",
                      "p.filament": "", "materials": "PLA black\n\nPETG grey\n",
                      "bed_type": ""})  # fmt: skip
        assert r.status_code == 303, r.text

        cfg = config.load(a.path, create=False)
        assert cfg.slicer_modules["gc"] == config.ModuleConfig(
            "gc", "test-gcode", {"url": "http://slicer.lan:3003"}
        )
        assert cfg.targets["voron"].kind == "moonraker"
        assert cfg.targets["voron"].values == {
            "url": "http://voron.lan:7125", "folder": "os2slice", "spoolman": False,
        }  # fmt: skip
        p = cfg.printers["x"]
        assert (p.target, p.slicer, p.model, p.bed_mm) == ("voron", "gc", "Voron 2.4",
                                                           (350.0, 350.5))  # fmt: skip
        assert p.profiles.printer == "Voron profile" and p.materials == ("PLA black", "PETG grey")
        assert "profiles = { " in a.path.read_text()  # inline, as in MODULES.md
        assert a.path.read_text().startswith("# os2slice config, written by its config page")

        # reloaded: new modules swapped in, the old ones replaced
        assert a.svc.modules is not old
        assert isinstance(a.svc.modules.targets["voron"], Moonraker)
        assert isinstance(a.svc.modules.slicers["gc"], GcodeSlicer)
        assert a.svc.cfg.printers == cfg.printers
        assert "x" in [pr.key for pr in a.svc.modules.printers()]

        # the list pages show them with health from check()
        page = a.get("/admin/targets").text
        assert "voron" in page and "Moonraker v0.9.3" in page
        page = a.get("/admin/printers/edit?key=x").text
        assert 'list="dl-printer"' in page and "Voron profile" in page  # suggestions

        # remove: a target a printer still uses is refused, the printer first works
        r = a.submit("/admin/targets", "/admin/targets/remove", {"key": "voron"})
        assert r.status_code == 400 and "voron" in r.text
        r = a.submit("/admin/printers", "/admin/printers/remove", {"key": "x"})
        assert r.status_code == 303
        r = a.submit("/admin/targets", "/admin/targets/remove", {"key": "voron"})
        assert r.status_code == 303
        assert "voron" not in config.load(a.path, create=False).targets
    finally:
        a.close()


def test_validation_failure_rerenders_without_writing(logged: Admin) -> None:
    before = logged.path.read_bytes()
    r = logged.submit("/admin/targets/new?kind=moonraker", "/admin/targets/save",
                      {"kind": "moonraker", "mode": "new", "key": "voron",
                       "f.url": "not a url <b>", "do": "save"})  # fmt: skip
    assert r.status_code == 400
    assert "targets.voron.url must be" in r.text
    assert "not a url &lt;b&gt;" in r.text  # the user's value, escaped
    assert logged.path.read_bytes() == before
    r = logged.submit("/admin/targets/new?kind=moonraker", "/admin/targets/save",
                      {"kind": "moonraker", "mode": "new", "key": "Bad Name",
                       "f.url": "http://voron.lan:7125", "do": "save"})  # fmt: skip
    assert r.status_code == 400 and "a-z, 0-9" in r.text
    r = logged.submit("/admin/server", "/admin/server",
                      {"bind": "0.0.0.0", "port": "8765", "hosts": "localhost:8765",
                       "identity": "none", "allowed_users": "", "tls_cert": "",
                       "tls_key": ""})  # fmt: skip
    assert r.status_code == 400 and "Only identity = &quot;lan&quot;" in r.text
    assert logged.path.read_bytes() == before


def test_unknown_form_fields_are_refused(logged: Admin) -> None:
    r = logged.submit("/admin/defaults", "/admin/defaults", {"walls": "3", "evil": "1"})
    assert r.status_code == 400


def test_secrets_are_never_rendered_or_written(tmp_path: Path, monkeypatch) -> None:
    stored: dict[str, str] = {}
    monkeypatch.setattr("keyring.set_password", lambda s, n, v: stored.__setitem__(n, v))
    a = Admin(tmp_path, secrets=lambda name: SECRET)
    try:
        a.login()
        r = a.submit("/admin/targets/new?kind=moonraker", "/admin/targets/save",
                     {"kind": "moonraker", "mode": "new", "key": "voron",
                      "f.url": "http://voron.lan:7125", "s.api_key": SECRET + "2",
                      "do": "save"})  # fmt: skip
        assert r.status_code == 303
        assert stored == {"targets.voron.api_key": SECRET + "2"}
        r = a.submit("/admin/secrets", "/admin/secrets",
                     {"name": "targets.farm.api_key", "value": SECRET + "3"})  # fmt: skip
        assert r.status_code == 303 and stored["targets.farm.api_key"] == SECRET + "3"
        # a failing save re-renders the form: still no secret in it
        r = a.submit("/admin/targets/edit?key=voron", "/admin/targets/save",
                     {"kind": "moonraker", "mode": "edit", "key": "voron",
                      "f.url": "bad", "s.api_key": SECRET + "4", "do": "save"})  # fmt: skip
        assert r.status_code == 400
        for path in [
            *admin.GET_ROUTES,
            "/admin/targets/edit?key=voron",
            "/admin/targets/edit?key=farm",
            "/admin/targets/new?kind=bambuddy",
        ]:
            a.get(path)
        assert len(a.pages) > 20
        assert not any(SECRET in page for page in a.pages)
        assert "set" in a.get("/admin/secrets").text
        assert SECRET not in a.path.read_text()
        r = a.submit("/admin/secrets", "/admin/secrets", {"name": "nope.x.y", "value": "v"})
        assert r.status_code == 400
    finally:
        a.close()


def test_test_connection_shows_health_without_saving(tmp_path: Path) -> None:
    a = Admin(tmp_path, transports={"voron": httpx.MockTransport(FakeMoonraker())})
    try:
        a.login()
        before = a.path.read_bytes()
        r = a.submit("/admin/targets/new?kind=moonraker", "/admin/targets/save",
                     {"kind": "moonraker", "mode": "new", "key": "voron",
                      "f.url": "http://voron.lan:7125", "do": "test"})  # fmt: skip
        assert r.status_code == 200
        assert "Test connection: Moonraker v0.9.3" in r.text and "ready" in r.text
        assert 'value="http://voron.lan:7125"' in r.text  # the form is kept
        assert a.path.read_bytes() == before and "voron" not in a.svc.modules.targets
        down = FakeMoonraker(klippy_state="shutdown")
        a.svc.module_transports["voron"] = httpx.MockTransport(down)
        r = a.submit("/admin/targets/new?kind=moonraker", "/admin/targets/save",
                     {"kind": "moonraker", "mode": "new", "key": "voron",
                      "f.url": "http://voron.lan:7125", "do": "test"})  # fmt: skip
        assert "Klipper is shutdown" in r.text
    finally:
        a.close()


def test_edit_discovering_target_models(logged: Admin) -> None:
    page = logged.get("/admin/targets/edit?key=farm").text
    assert 'name="m0.model" value="A1 Mini"' in page and 'name="m1.model" value=""' in page
    r = logged.submit("/admin/targets/edit?key=farm", "/admin/targets/save",
                      {"kind": "bambuddy", "mode": "edit", "key": "farm",
                       "f.url": "http://bambuddy.test:8000", "f.folder": "Prints",
                       "f.manual_start": "on", "do": "save",
                       "m0.model": "A1 Mini", "m0.slicer": "", "m0.printer": "P",
                       "m0.process": "Q", "m0.filament": "F", "m0.bed_type": "",
                       "m1.model": "X1C", "m1.slicer": "", "m1.printer": "P2",
                       "m1.process": "Q2", "m1.filament": "F2",
                       "m1.bed_type": "Textured PEI Plate"})  # fmt: skip
    assert r.status_code == 303, r.text
    t = config.load(logged.path, create=False).targets["farm"]
    assert t.values["folder"] == "Prints"
    assert set(t.models) == {"A1 Mini", "X1C"}
    assert t.models["X1C"].bed_type == "Textured PEI Plate"
    assert logged.svc.modules.targets["farm"].folder == "Prints"


# ---- legacy ----------------------------------------------------------------------


def test_migrate_legacy_parses_identically(tmp_path: Path) -> None:
    text = LEGACY.format(exports=tmp_path / "exports")
    data = tomllib.loads(text)
    before = config.parse(tomllib.loads(text), tmp_path / "c.toml")
    admin.migrate_legacy(data)
    assert "bambuddy" not in data and data["targets"]["bambuddy"]["kind"] == "bambuddy"
    after = config.parse(tomllib.loads(admin.render_toml(data)), tmp_path / "c.toml")
    assert after == before


def test_migrate_through_the_page(tmp_path: Path) -> None:
    a = Admin(tmp_path, LEGACY)
    try:
        a.login()
        before = config.load(a.path, create=False)
        page = a.get("/admin/targets").text
        assert "bambuddy (legacy table)" in page
        r = a.submit("/admin/targets", "/admin/targets/migrate", {})
        assert r.status_code == 303, r.text
        raw = tomllib.loads(a.path.read_text())
        assert "bambuddy" not in raw and "bambuddy" in raw["targets"]
        assert config.load(a.path, create=False) == before
        assert "legacy table" not in a.get("/admin/targets").text
    finally:
        a.close()


# ---- other pages -------------------------------------------------------------------


def test_server_changes_need_a_restart(logged: Admin) -> None:
    r = logged.submit("/admin/server", "/admin/server",
                      {"bind": "127.0.0.1", "port": "8766",
                       "hosts": "localhost:8765\nlocalhost:8766", "identity": "none",
                       "allowed_users": "", "tls_cert": "", "tls_key": ""})  # fmt: skip
    assert r.status_code == 303, r.text
    assert config.load(logged.path, create=False).server.port == 8766
    assert logged.svc.cfg.server.port == 8765  # still what the socket uses
    assert "restart the service" in logged.get("/admin").text


def test_overview_doctor_and_onshape(logged: Admin) -> None:
    page = logged.get("/admin").text
    assert "slicer + target farm" in page and "BamBuddy" in page
    assert "API keys" in page and str(logged.path) in page
    page = logged.get("/admin/onshape").text
    assert "https://cad.onshape.com" in page and "not set" in page
    r = logged.submit("/admin/onshape", "/admin/onshape",
                      {"base_url": "https://cad.onshape.com", "auth": "keys",
                       "oauth_client_id": "", "oauth_url": "", "oauth_client_secret": "",
                       "access_key": "only-one", "secret_key": ""})  # fmt: skip
    assert r.status_code == 400 and "both API keys" in r.text
    r = logged.submit("/admin/onshape", "/admin/onshape",
                      {"base_url": "https://evil.example.com", "auth": "keys",
                       "oauth_client_id": "", "oauth_url": "", "oauth_client_secret": "",
                       "access_key": "", "secret_key": ""})  # fmt: skip
    assert r.status_code == 400 and "onshape.base_url" in r.text


def test_printers_override_and_default(logged: Admin) -> None:
    page = logged.get("/admin/printers").text
    assert "X1C_01" in page and "/admin/printers/new?name=X1C_01" in page
    page = logged.get("/admin/printers/new?name=X1C_01").text
    assert 'name="key" value="X1C_01"' in page
    r = logged.submit("/admin/printers/new?name=X1C_01", "/admin/printers/save",
                      {"mode": "new", "key": "X1C_01", "target": "", "slicer": "",
                       "bed_type": "Engineering Plate"})  # fmt: skip
    assert r.status_code == 303, r.text
    assert config.load(logged.path, create=False).printers["X1C_01"].bed_type == (
        "Engineering Plate"
    )
    r = logged.submit("/admin/printers", "/admin/printers/default",
                      {"default_printer": "X1C_01"})  # fmt: skip
    assert r.status_code == 303
    assert logged.svc.cfg.default_printer == "X1C_01"


def test_jobs_show_every_user(logged: Admin) -> None:
    done = threading.Event()

    def work(progress: Callable[[str], None]) -> list[str]:
        progress("Exported <b>")
        done.set()
        return []

    logged.svc.jobs.start("onshape:someone", "Part on printer A1 Mini", work)
    done.wait(2)
    page = logged.get("/admin/jobs").text
    assert "onshape:someone" in page and "Exported &lt;b&gt;" in page


def test_log_tail_is_escaped(logged: Admin) -> None:
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"line {n}\n" for n in range(300)) + "<script>alert(1)</script>\n")
    page = logged.get("/admin/log").text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page and "<script>alert" not in page
    assert "line 299" in page and "line 99\n" not in page  # only the last 200
    assert len(admin.log_tail()) == 200


def test_pages_keep_the_strict_csp(logged: Admin) -> None:
    r = logged.get("/admin/targets")
    csp = r.headers["Content-Security-Policy"]
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp
    assert "script-src" not in csp and "<script" not in r.text
    assert r.headers["Cache-Control"] == "no-store"
    assert f"Editing {logged.path}" in r.text


# ---- CLI -------------------------------------------------------------------------


def test_cli_admin_password_and_doctor_note(monkeypatch: pytest.MonkeyPatch) -> None:
    rows: list[tuple[str, str, str]] = []
    cli.check_admin_password(lambda s, c, d: rows.append((s, c, d)))
    assert rows[-1][0] == cli.NOTE and "admin-password" in rows[-1][2]
    answers = iter([PASSWORD, PASSWORD])
    real = adminauth.set_password_interactive
    monkeypatch.setattr(
        adminauth, "set_password_interactive",
        lambda store: real(store, prompt=lambda _p: next(answers)),
    )  # fmt: skip
    assert cli.main(["admin-password"]) == 0
    assert AdminStore(adminauth.admin_path()).verify(PASSWORD)
    cli.check_admin_password(lambda s, c, d: rows.append((s, c, d)))
    assert rows[-1][0] == cli.PASS
