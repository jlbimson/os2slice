"""Secrets: the system keyring, a 0600 file where there is no keyring, then the environment.

Onshape API keys and the OAuth client secret have their own entries. Module secrets
(docs/MODULES.md) are named "<section>.<key>.<field>", e.g. "targets.farm.api_key":
the keyring entry has that name, the environment variable is OS2SLICE_SECRET_ plus
the name upper-cased with dots and dashes as underscores.

**Where secrets are saved.** In the system keyring, unless the file store is active:
when `OS2SLICE_ADDON=1` (the Home Assistant add-on and the Docker image), or when the
keyring backend is the null or fail backend (`PYTHON_KEYRING_BACKEND` names one, or
`keyring` found no usable backend). Then every `store_*` writes
`<state dir>/secrets.json` (`secrets_path()`: a JSON object of entry name to value,
0600, replaced atomically) and never the keyring. A keyring that exists but refuses
(a locked wallet) is still an error: on a desktop os2slice doesn't quietly fall back
to a plain file.

**Lookup order** for every secret (`find_secret`, `load_keys`,
`load_oauth_client_secret`):

1. the keyring (skipped while the file store is active),
2. `secrets.json` (when it exists),
3. the environment.

Within each, an entry's own name comes before its legacy name (`LEGACY_SECRETS`: the
BamBuddy key was keyring entry `bambuddy_api_key` / `$BAMBUDDY_API_KEY`). The Onshape
key pair is always taken whole from one place.

**In the add-on** (`addon.prepare`) the file comes before the environment, so a secret
saved on the config page `/admin` is used while the matching add-on option is empty.
A non-empty option wins (the options are the operator's source of truth): at every
start the add-on exports it to the environment *and* deletes the same entries from
`secrets.json` (`SecretFile.delete`). A value saved on the page while the option is
filled in is used until the next start, when the option replaces it again.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import keyring
import keyring.errors
from keyring.backends import fail as keyring_fail
from keyring.backends import null as keyring_null

from os2slice.errors import AuthError
from os2slice.logsetup import state_dir
from os2slice.runtime import DOCKER, HOME_ASSISTANT, runtime
from os2slice.tomlwrite import write_atomic

log = logging.getLogger(__name__)

SERVICE = "os2slice"
ACCESS_ENTRY = "access_key"
SECRET_ENTRY = "secret_key"  # noqa: S105 - keyring entry name, not a secret
ENV_ACCESS = "ONSHAPE_ACCESS_KEY"
ENV_SECRET = "ONSHAPE_SECRET_KEY"  # noqa: S105 - env var name
BAMBUDDY_ENTRY = "bambuddy_api_key"
ENV_BAMBUDDY = "BAMBUDDY_API_KEY"
OAUTH_SECRET_ENTRY = "oauth_client_secret"  # noqa: S105 - keyring entry name
ENV_OAUTH_SECRET = "ONSHAPE_OAUTH_CLIENT_SECRET"  # noqa: S105 - env var name
SECRET_NAME_RE = re.compile(r"[a-z_]{1,20}\.[a-z0-9_-]{1,40}\.[a-z0-9_]{1,40}")
ENV_SECRET_PREFIX = "OS2SLICE_SECRET_"  # noqa: S105 - env var prefix
BAMBUDDY_SECRET = "targets.bambuddy.api_key"  # noqa: S105 - secret name
# Where these secrets lived before modules (keyring entry, env var); still read.
LEGACY_SECRETS = {BAMBUDDY_SECRET: (BAMBUDDY_ENTRY, ENV_BAMBUDDY)}
SECRETS_FILE = "secrets.json"
NO_KEYRING_BACKENDS = ("keyring.backends.null.Keyring", "keyring.backends.fail.Keyring")
_FILE_LOCK = threading.Lock()


def secrets_path() -> Path:
    """The file store: `<state dir>/secrets.json`."""
    return state_dir() / SECRETS_FILE


def file_store_active() -> bool:
    """True when secrets are saved in `secrets.json` instead of the system keyring."""
    if os.environ.get("OS2SLICE_ADDON") == "1":
        return True
    if os.environ.get("PYTHON_KEYRING_BACKEND", "").strip() in NO_KEYRING_BACKENDS:
        return True
    try:
        backend = _backend()
    except Exception:  # a broken keyring install is as good as none
        return True
    return isinstance(backend, (keyring_null.Keyring, keyring_fail.Keyring))


def _backend() -> object:
    """The keyring backend in use (a seam for tests, which never touch the real one)."""
    return keyring.get_keyring()


def secret_store_name() -> str:
    """Where `store_*` saves, for messages: the system keyring or the file's path."""
    return f"the secret file {secrets_path()}" if file_store_active() else "the system keyring"


class SecretFile:
    """A JSON object of entry name → value in a 0600 file, replaced atomically.

    Nothing here logs a value. A file that can't be parsed reads as empty (and is
    logged), and refuses writes, so a damaged file is never silently replaced.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _load(self) -> dict[str, str]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        data = json.loads(text)
        if not isinstance(data, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in data.items()
        ):
            raise ValueError("not a JSON object of strings")
        return data

    def read(self) -> dict[str, str]:
        try:
            return self._load()
        except (OSError, ValueError) as e:
            log.error("can't read the secret file %s: %s", self.path, type(e).__name__)
            return {}

    def get(self, *entries: str) -> str | None:
        """The first of `entries` that has a value."""
        data = self.read()
        for entry in entries:
            value = data.get(entry) if entry else None
            if value:
                return value
        return None

    def has(self, *entries: str) -> bool:
        """True when every one of `entries` has a value."""
        data = self.read()
        return all(data.get(e) for e in entries)

    def _write(self, data: dict[str, str]) -> None:
        write_atomic(self.path, json.dumps(data, indent=1, sort_keys=True) + "\n", mode=0o600)

    def set(self, values: dict[str, str]) -> None:
        with _FILE_LOCK:
            try:
                data = self._load()
                data.update(values)
                self._write(data)
            except (OSError, ValueError) as e:
                raise AuthError(
                    f"Couldn't save to the secret file {self.path} ({type(e).__name__})",
                    "Check that the file is valid JSON and its directory is writable",
                ) from e

    def delete(self, entries: Iterable[str]) -> list[str]:
        """Remove these entries; returns the ones that were there."""
        with _FILE_LOCK:
            try:
                data = self._load()
            except (OSError, ValueError) as e:
                log.error("can't read the secret file %s: %s", self.path, type(e).__name__)
                return []
            gone = [e for e in entries if e in data]
            for e in gone:
                del data[e]
            if gone:
                self._write(data)
            return gone


def _file() -> SecretFile:
    return SecretFile(secrets_path())


def _keyring_get(entry: str) -> str | None:
    """A keyring entry; None when the file store is active or the keyring fails."""
    if not entry or file_store_active():
        return None
    try:
        return keyring.get_password(SERVICE, entry) or None
    except keyring.errors.KeyringError as e:
        log.warning("keyring read failed: %s", type(e).__name__)
        return None


def _store(values: dict[str, str], what: str) -> None:
    """Save entries in the file store when it's active, else in the system keyring."""
    if file_store_active():
        _file().set(values)
        return
    try:
        for entry, value in values.items():
            keyring.set_password(SERVICE, entry, value)
    except keyring.errors.KeyringError as e:
        raise AuthError(
            f"Couldn't save {what} to the system keyring ({type(e).__name__})",
            "Make sure KDE Wallet or GNOME Keyring is running and unlocked",
        ) from e


@dataclass(frozen=True)
class Keys:
    access: str
    secret: str = field(repr=False)
    source: str = "keyring"


def load_keys() -> Keys:
    """The Onshape API key pair: keyring → secret file → environment, whole pair each."""
    keyring_problem = ""
    if not file_store_active():
        try:
            access = keyring.get_password(SERVICE, ACCESS_ENTRY)
            secret = keyring.get_password(SERVICE, SECRET_ENTRY)
            if access and secret:
                return Keys(access, secret, "keyring")
        except keyring.errors.KeyringError as e:
            keyring_problem = f" (keyring unavailable: {type(e).__name__})"
            log.warning("keyring read failed: %s", type(e).__name__)

    stored = _file().read()
    access, secret = stored.get(ACCESS_ENTRY), stored.get(SECRET_ENTRY)
    if access and secret:
        return Keys(access, secret, "file")

    access, secret = os.environ.get(ENV_ACCESS), os.environ.get(ENV_SECRET)
    if access and secret:
        return Keys(access, secret, "environment")
    raise AuthError(f"No Onshape API keys found{keyring_problem}", "Run `os2slice setup-keys`")


def store_keys(access: str, secret: str) -> None:
    _store({ACCESS_ENTRY: access, SECRET_ENTRY: secret}, "keys")


def delete_keys() -> None:
    if not file_store_active():
        for entry in (ACCESS_ENTRY, SECRET_ENTRY):
            with contextlib.suppress(keyring.errors.PasswordDeleteError):
                keyring.delete_password(SERVICE, entry)
    _file().delete((ACCESS_ENTRY, SECRET_ENTRY))


def secret_env(name: str) -> str:
    """The environment variable for a module secret: targets.farm.api_key →
    OS2SLICE_SECRET_TARGETS_FARM_API_KEY."""
    return ENV_SECRET_PREFIX + re.sub(r"[.-]", "_", name).upper()


def _check_name(name: str) -> None:
    if not SECRET_NAME_RE.fullmatch(name):
        raise AuthError(
            f"Invalid secret name {name[:60]!r}",
            "Use <section>.<key>.<field>, e.g. targets.x.api_key",
        )


def find_secret(name: str) -> tuple[str | None, str]:
    """(value, source) of a module secret; (None, "") when it isn't stored anywhere.

    source is "keyring", "file" or "environment" (see the module docstring for the order).
    """
    _check_name(name)
    legacy_entry, legacy_env = LEGACY_SECRETS.get(name, ("", ""))
    for entry in (name, legacy_entry):
        value = _keyring_get(entry)
        if value:
            return value, "keyring"
    value = _file().get(name, legacy_entry)
    if value:
        return value, "file"
    for env in (secret_env(name), legacy_env):
        value = os.environ.get(env, "") if env else ""
        if value:
            return value, "environment"
    return None, ""


def get_secret(name: str) -> str | None:
    """A module secret by name (keyring, secret file, environment), or None."""
    return find_secret(name)[0]


def store_secret(name: str, value: str) -> None:
    """Save a module secret under its name (keyring, or the secret file; see above)."""
    _check_name(name)
    _store({name: value}, name)


def load_bambuddy_key() -> tuple[str, str]:
    """(key, source) of the BamBuddy key ([bambuddy] / [targets.bambuddy])."""
    key, source = find_secret(BAMBUDDY_SECRET)
    if key:
        return key, source
    raise AuthError("No BamBuddy API key found", "Run `os2slice setup-keys --bambuddy`")


def store_bambuddy_key(key: str) -> None:
    store_secret(BAMBUDDY_SECRET, key)


def store_oauth_client_secret(secret: str) -> None:
    """Save the OAuth app's client secret (D-23)."""
    _store({OAUTH_SECRET_ENTRY: secret}, "the OAuth client secret")


def has_oauth_client_secret() -> bool:
    try:
        load_oauth_client_secret()
    except AuthError:
        return False
    return True


def load_oauth_client_secret() -> str:
    """The OAuth app's client secret (D-23): keyring → secret file →
    $ONSHAPE_OAUTH_CLIENT_SECRET."""
    secret = _keyring_get(OAUTH_SECRET_ENTRY) or _file().get(OAUTH_SECRET_ENTRY)
    if secret:
        return secret
    secret = os.environ.get(ENV_OAUTH_SECRET, "")
    if secret:
        return secret
    where = runtime()
    if where == HOME_ASSISTANT:
        fix = "Set onshape_oauth_client_secret on the add-on's Configuration tab"
    elif where == DOCKER:
        fix = f"Set {ENV_OAUTH_SECRET} in .env, then run docker compose up -d"
    else:
        fix = f"Run os2slice setup-keys --secret {OAUTH_SECRET_ENTRY}, or set ${ENV_OAUTH_SECRET}"
    raise AuthError("No Onshape OAuth client secret found", fix)
