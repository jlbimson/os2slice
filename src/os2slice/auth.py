"""Secrets: system keyring first, environment variables as a dev / add-on fallback.

Onshape API keys and the OAuth client secret have their own entries. Module secrets
(docs/MODULES.md) are named "<section>.<key>.<field>", e.g. "targets.farm.api_key":
the keyring entry has that name, the environment variable is OS2SLICE_SECRET_ plus
the name upper-cased with dots and dashes as underscores.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
from dataclasses import dataclass, field

import keyring
import keyring.errors

from os2slice.errors import AuthError

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


@dataclass(frozen=True)
class Keys:
    access: str
    secret: str = field(repr=False)
    source: str = "keyring"


def load_keys() -> Keys:
    keyring_problem = ""
    try:
        access = keyring.get_password(SERVICE, ACCESS_ENTRY)
        secret = keyring.get_password(SERVICE, SECRET_ENTRY)
        if access and secret:
            return Keys(access, secret, "keyring")
    except keyring.errors.KeyringError as e:
        keyring_problem = f" (keyring unavailable: {type(e).__name__})"
        log.warning("keyring read failed: %s", type(e).__name__)

    access, secret = os.environ.get(ENV_ACCESS), os.environ.get(ENV_SECRET)
    if access and secret:
        return Keys(access, secret, "environment")
    raise AuthError(f"No Onshape API keys found{keyring_problem}", "Run `os2slice setup-keys`")


def store_keys(access: str, secret: str) -> None:
    try:
        keyring.set_password(SERVICE, ACCESS_ENTRY, access)
        keyring.set_password(SERVICE, SECRET_ENTRY, secret)
    except keyring.errors.KeyringError as e:
        raise AuthError(
            f"Couldn't save keys to the system keyring ({type(e).__name__})",
            "Make sure KDE Wallet or GNOME Keyring is running and unlocked",
        ) from e


def delete_keys() -> None:
    for entry in (ACCESS_ENTRY, SECRET_ENTRY):
        with contextlib.suppress(keyring.errors.PasswordDeleteError):
            keyring.delete_password(SERVICE, entry)


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
    """(value, source) of a module secret; (None, "") when it isn't stored anywhere."""
    _check_name(name)
    legacy_entry, legacy_env = LEGACY_SECRETS.get(name, ("", ""))
    try:
        for entry in (name, legacy_entry):
            value = keyring.get_password(SERVICE, entry) if entry else None
            if value:
                return value, "keyring"
    except keyring.errors.KeyringError as e:
        log.warning("keyring read failed: %s", type(e).__name__)
    for env in (secret_env(name), legacy_env):
        value = os.environ.get(env, "") if env else ""
        if value:
            return value, "environment"
    return None, ""


def get_secret(name: str) -> str | None:
    """A module secret by name (keyring, then environment), or None."""
    return find_secret(name)[0]


def store_secret(name: str, value: str) -> None:
    """Save a module secret in the system keyring under its name."""
    _check_name(name)
    try:
        keyring.set_password(SERVICE, name, value)
    except keyring.errors.KeyringError as e:
        raise AuthError(
            f"Couldn't save {name} to the system keyring ({type(e).__name__})",
            "Make sure KDE Wallet or GNOME Keyring is running and unlocked",
        ) from e


def load_bambuddy_key() -> tuple[str, str]:
    """(key, source) of the BamBuddy key ([bambuddy] / [targets.bambuddy])."""
    key, source = find_secret(BAMBUDDY_SECRET)
    if key:
        return key, source
    raise AuthError("No BamBuddy API key found", "Run `os2slice setup-keys --bambuddy`")


def store_bambuddy_key(key: str) -> None:
    store_secret(BAMBUDDY_SECRET, key)


def load_oauth_client_secret() -> str:
    """The OAuth app's client secret (D-23). Keyring first, then $ONSHAPE_OAUTH_CLIENT_SECRET."""
    try:
        secret = keyring.get_password(SERVICE, OAUTH_SECRET_ENTRY)
        if secret:
            return secret
    except keyring.errors.KeyringError as e:
        log.warning("keyring read failed: %s", type(e).__name__)
    secret = os.environ.get(ENV_OAUTH_SECRET, "")
    if secret:
        return secret
    raise AuthError(
        "No Onshape OAuth client secret found",
        "Set onshape_oauth_client_secret on the add-on's Configuration tab",
    )
