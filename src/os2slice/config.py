"""Load and validate ~/.config/os2slice/config.toml."""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, cast

from os2slice.bambuddy import PresetChoice
from os2slice.errors import BadRequest, ConfigError
from os2slice.request import FORMATS, SLICER_KEY_RE, Fmt
from os2slice.settings import PrintSettings, check_bed_type

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

ONSHAPE_BASE_RE = re.compile(r"https://[a-z0-9-]+\.onshape\.com")
HTTP_BASE_RE = re.compile(r"https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?")
PRESET_SOURCES = ("standard", "cloud", "local", "orca_cloud")
IP_RE = re.compile(r"(\d{1,3}\.){3}\d{1,3}")
HOST_RE = re.compile(r"[A-Za-z0-9.-]+(:[0-9]{1,5})?|\[::1\](:[0-9]{1,5})?")
UNITS = ("millimeter", "centimeter", "meter", "inch", "foot", "yard")
FIX = "Edit {path}"


@dataclass(frozen=True)
class SlicerConfig:
    key: str
    name: str
    argv: tuple[str, ...]


@dataclass(frozen=True)
class Config:
    path: Path
    onshape_base_url: str
    port: int
    export_dir: Path
    export_format: Fmt
    units: str
    keep_days: int
    slicers: dict[str, SlicerConfig]
    bambuddy: BambuddyConfig | None = None
    print_defaults: PrintSettings = field(default_factory=PrintSettings)
    default_bed_type: str | None = None  # [print_defaults] bed_type
    server: ServerConfig = field(default_factory=lambda: ServerConfig())
    web_studio: WebStudioConfig | None = None
    # [onshape] auth: "keys" = one shared API key pair; "oauth" = each user signs in (D-23)
    onshape_auth: str = "keys"
    oauth_client_id: str = ""
    oauth_base_url: str = "https://oauth.onshape.com"

    @property
    def oauth_redirect_uri(self) -> str:
        """Where Onshape sends users back; register it on the OAuth app."""
        return f"https://{self.server.hosts[0]}/auth/callback"


@dataclass(frozen=True)
class WebStudioConfig:
    """The shared Bambu Studio browser session (bambustudio_web add-on, D-21)."""

    url: str  # what browsers open, e.g. https://print.example.duckdns.org:3001
    inbox: Path  # where os2slice drops 3MF files for it to open
    status: Path  # its {"viewers": n, "updated": t} file


@dataclass(frozen=True)
class ServerConfig:
    """How `os2slice serve` decides who may use it (D-11, D-13)."""

    bind: str = "127.0.0.1"
    port: int = 8765
    hosts: tuple[str, ...] = ("localhost:8765", "127.0.0.1:8765")  # accepted Host headers
    identity: str = "none"  # "none" (localhost only) | "lan" (D-17) | "tailscale" (D-11)
    allowed_users: tuple[str, ...] = ()  # Tailscale logins, when identity = "tailscale"
    tls_cert: Path | None = None  # PEM chain; required for "lan"
    tls_key: Path | None = None


@dataclass(frozen=True)
class BambuddyConfig:
    base_url: str
    folder: str
    default_printer: str
    manual_start: bool
    presets: dict[str, PresetChoice]  # keyed by BamBuddy printer model, e.g. "A1 Mini"
    public_url: str = ""  # BamBuddy's UI as browsers reach it (default: this host, port 8000)


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "os2slice"


def config_path() -> Path:
    return config_dir() / "config.toml"


def default_config_text() -> str:
    return resources.files("os2slice").joinpath("default_config.toml").read_text("utf-8")


def load(path: Path | None = None, create: bool = True) -> Config:
    """Load the config, writing the default first if it doesn't exist yet."""
    path = path or config_path()
    if not path.exists():
        if not create:
            raise ConfigError(f"No config file at {path}", "Run any os2slice command to create it")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(default_config_text(), encoding="utf-8")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"Can't read {path}: {e}", FIX.format(path=path)) from e
    return parse(data, path)


def parse(data: dict[str, Any], path: Path) -> Config:
    fix = FIX.format(path=path)

    def fail(msg: str) -> ConfigError:
        return ConfigError(f"config.toml: {msg}", fix)

    def table(name: str) -> dict[str, Any]:
        value = data.get(name, {})
        if not isinstance(value, dict):
            raise fail(f"[{name}] must be a table")
        return value

    def check_keys(name: str, tbl: dict[str, Any], allowed: set[str]) -> None:
        unknown = sorted(set(tbl) - allowed)
        if unknown:
            raise fail(f"unknown key(s) in [{name}]: {', '.join(unknown)}")

    unknown_tables = sorted(
        set(data)
        - {"onshape", "server", "export", "slicers", "bambuddy", "print_defaults", "web_studio"}
    )
    if unknown_tables:
        raise fail(f"unknown table(s): {', '.join(unknown_tables)}")

    onshape = table("onshape")
    check_keys("onshape", onshape, {"base_url", "auth", "oauth_client_id", "oauth_url"})
    base_url = str(onshape.get("base_url", "https://cad.onshape.com")).rstrip("/")
    if not ONSHAPE_BASE_RE.fullmatch(base_url):
        raise fail("onshape.base_url must look like https://<name>.onshape.com")
    onshape_auth = onshape.get("auth", "keys")
    if onshape_auth not in ("keys", "oauth"):
        raise fail('onshape.auth must be "keys" or "oauth"')
    oauth_client_id = str(onshape.get("oauth_client_id", ""))
    oauth_base_url = str(onshape.get("oauth_url", "https://oauth.onshape.com")).rstrip("/")
    if not ONSHAPE_BASE_RE.fullmatch(oauth_base_url):
        raise fail("onshape.oauth_url must look like https://<name>.onshape.com")
    if onshape_auth == "oauth" and not re.fullmatch(r"[A-Za-z0-9=+/._-]{8,200}", oauth_client_id):
        raise fail('onshape.oauth_client_id is required when auth = "oauth"')

    server = table("server")
    check_keys(
        "server",
        server,
        {"port", "bind", "hosts", "identity", "allowed_users", "tls_cert", "tls_key"},
    )
    port = server.get("port", 8765)
    if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
        raise fail("server.port must be an integer from 1024 to 65535")
    bind = server.get("bind", "127.0.0.1")
    if not isinstance(bind, str) or not IP_RE.fullmatch(bind):
        raise fail("server.bind must be an IPv4 address such as 127.0.0.1 or 0.0.0.0")
    hosts = server.get("hosts", [f"localhost:{port}", f"127.0.0.1:{port}"])
    if not (isinstance(hosts, list) and hosts and all(HOST_RE.fullmatch(str(h)) for h in hosts)):
        raise fail("server.hosts must be a list of host[:port] names")
    identity = server.get("identity", "none")
    if identity not in ("none", "lan", "tailscale"):
        raise fail('server.identity must be "none", "lan" or "tailscale"')
    users = server.get("allowed_users", [])
    if not (isinstance(users, list) and all(isinstance(u, str) and "@" in u for u in users)):
        raise fail("server.allowed_users must be a list of Tailscale logins")
    if identity == "tailscale" and not users:
        raise fail('server.allowed_users can\'t be empty when identity = "tailscale"')
    if identity == "none" and any(not _is_local_host(str(h)) for h in hosts):
        raise fail('identity = "none" is only allowed when every server.hosts entry is localhost')
    tls = [server.get(k) for k in ("tls_cert", "tls_key")]
    if any(v is not None and (not isinstance(v, str) or not v) for v in tls) or (
        (tls[0] is None) != (tls[1] is None)
    ):
        raise fail("server.tls_cert and server.tls_key must both be file paths, or both unset")
    if onshape_auth == "oauth" and (identity != "lan" or tls[0] is None):
        # Sign-in cookies are Secure and Onshape needs an https redirect URI.
        raise fail('onshape.auth = "oauth" needs server.identity = "lan" with tls_cert/tls_key')
    tls_cert, tls_key = (Path(v).expanduser() if v else None for v in tls)
    if bind not in ("127.0.0.1",) and identity != "lan":
        raise fail('Only identity = "lan" may bind beyond 127.0.0.1')
    if identity == "lan":
        if tls_cert is None:
            raise fail('identity = "lan" needs server.tls_cert and server.tls_key (HTTPS)')
        if "hosts" not in server:
            raise fail('identity = "lan" needs server.hosts, e.g. ["<name>.duckdns.org:8443"]')
    server_cfg = ServerConfig(
        bind, port, tuple(map(str, hosts)), identity, tuple(users), tls_cert, tls_key
    )

    export = table("export")
    check_keys("export", export, {"dir", "format", "units", "keep_days"})
    export_dir = export.get("dir", "~/OnshapeExports")
    if not isinstance(export_dir, str) or not export_dir:
        raise fail("export.dir must be a path")
    export_path = Path(export_dir).expanduser()
    if not export_path.is_absolute():
        raise fail("export.dir must be an absolute path (or start with ~)")
    fmt = export.get("format", "stl")
    if fmt not in FORMATS:
        raise fail(f"export.format must be one of {', '.join(FORMATS)}")
    units = export.get("units", "millimeter")
    if units not in UNITS:
        raise fail(f"export.units must be one of {', '.join(UNITS)}")
    keep_days = export.get("keep_days", 30)
    if not isinstance(keep_days, int) or isinstance(keep_days, bool) or keep_days < 0:
        raise fail("export.keep_days must be a whole number ≥ 0")

    slicers: dict[str, SlicerConfig] = {}
    for key, entry in table("slicers").items():
        where = f"slicers.{key}"
        if not SLICER_KEY_RE.fullmatch(key):
            raise fail(f"[{where}]: names may only use a-z, 0-9, _ and -")
        if not isinstance(entry, dict):
            raise fail(f"[{where}] must be a table")
        check_keys(where, entry, {"name", "argv"})
        argv = entry.get("argv")
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(a, str) and a for a in argv)
        ):
            raise fail(f"{where}.argv must be a non-empty list of strings")
        name = entry.get("name", key)
        if not isinstance(name, str) or not name:
            raise fail(f"{where}.name must be a string")
        slicers[key] = SlicerConfig(key=key, name=name, argv=tuple(argv))

    bambuddy = _parse_bambuddy(data, fail) if "bambuddy" in data else None

    pd = table("print_defaults")
    check_keys(
        "print_defaults", pd, {"walls", "infill", "supports", "build_plate_only", "bed_type"}
    )
    try:
        print_defaults = PrintSettings(**{k: pd[k] for k in pd if k != "bed_type"})
        default_bed_type = check_bed_type(pd.get("bed_type"))
    except BadRequest as e:
        raise fail(f"[print_defaults]: {e.message}") from e

    web_studio = None
    ws = table("web_studio")
    if ws:
        check_keys("web_studio", ws, {"url", "inbox", "status"})
        url = str(ws.get("url", "")).rstrip("/")
        if not re.fullmatch(r"https://[A-Za-z0-9.-]+(:[0-9]{1,5})?", url):
            raise fail("web_studio.url must look like https://host:3001")
        inbox = Path(str(ws.get("inbox", "/share/os2slice/inbox"))).expanduser()
        status = Path(str(ws.get("status", "/share/os2slice/web-studio.json"))).expanduser()
        if not (inbox.is_absolute() and status.is_absolute()):
            raise fail("web_studio.inbox and .status must be absolute paths")
        web_studio = WebStudioConfig(url, inbox, status)

    return Config(
        path=path,
        onshape_base_url=base_url,
        port=port,
        export_dir=export_path,
        export_format=cast(Fmt, fmt),
        units=units,
        keep_days=keep_days,
        slicers=slicers,
        bambuddy=bambuddy,
        print_defaults=print_defaults,
        default_bed_type=default_bed_type,
        server=server_cfg,
        web_studio=web_studio,
        onshape_auth=onshape_auth,
        oauth_client_id=oauth_client_id,
        oauth_base_url=oauth_base_url,
    )


def _is_local_host(host: str) -> bool:
    name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
    return name in ("localhost", "127.0.0.1", "[::1]")


def _parse_bambuddy(data: dict[str, Any], fail: Any) -> BambuddyConfig:
    bb = data["bambuddy"]
    if not isinstance(bb, dict):
        raise fail("[bambuddy] must be a table")
    unknown = sorted(
        set(bb) - {"base_url", "public_url", "folder", "default_printer", "manual_start", "presets"}
    )
    if unknown:
        raise fail(f"unknown key(s) in [bambuddy]: {', '.join(unknown)}")
    base_url = str(bb.get("base_url", "")).rstrip("/")
    if not HTTP_BASE_RE.fullmatch(base_url):
        raise fail("bambuddy.base_url must look like http://host:8000")
    public_url = str(bb.get("public_url", "")).rstrip("/")
    if public_url and not HTTP_BASE_RE.fullmatch(public_url):
        raise fail("bambuddy.public_url must look like http://host:8000")
    folder = bb.get("folder", "Onshape")
    if not isinstance(folder, str) or not folder or "/" in folder or len(folder) > 100:
        raise fail("bambuddy.folder must be a short folder name")
    default_printer = bb.get("default_printer", "")
    if not isinstance(default_printer, str):
        raise fail("bambuddy.default_printer must be a printer name")
    manual_start = bb.get("manual_start", True)
    if not isinstance(manual_start, bool):
        raise fail("bambuddy.manual_start must be true or false")
    presets: dict[str, PresetChoice] = {}
    raw = bb.get("presets", {})
    if not isinstance(raw, dict):
        raise fail("[bambuddy.presets] must be a table")
    for model, entry in raw.items():
        where = f"bambuddy.presets.{model}"
        if not isinstance(entry, dict):
            raise fail(f"[{where}] must be a table")
        extra = sorted(set(entry) - {"printer", "process", "filament", "source", "bed_type"})
        if extra:
            raise fail(f"unknown key(s) in [{where}]: {', '.join(extra)}")
        vals = {k: entry.get(k) for k in ("printer", "process", "filament")}
        if not all(isinstance(v, str) and v for v in vals.values()):
            raise fail(f"[{where}] needs printer, process and filament preset names")
        source = entry.get("source", "standard")
        if source not in PRESET_SOURCES:
            raise fail(f"{where}.source must be one of {', '.join(PRESET_SOURCES)}")
        try:
            bed = check_bed_type(entry.get("bed_type"))
        except BadRequest as e:
            raise fail(f"{where}.bed_type: {e.message}") from e
        presets[model] = PresetChoice(source=source, bed_type=bed, **vals)  # type: ignore[arg-type]
    return BambuddyConfig(base_url, folder, default_printer, manual_start, presets, public_url)
