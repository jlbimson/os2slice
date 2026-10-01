"""Onshape API keys: system keyring first, environment variables as a dev fallback."""

from __future__ import annotations

import contextlib
import logging
import os
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


def load_bambuddy_key() -> tuple[str, str]:
    """(key, source). Keyring first, then $BAMBUDDY_API_KEY."""
    try:
        key = keyring.get_password(SERVICE, BAMBUDDY_ENTRY)
        if key:
            return key, "keyring"
    except keyring.errors.KeyringError as e:
        log.warning("keyring read failed: %s", type(e).__name__)
    key = os.environ.get(ENV_BAMBUDDY, "")
    if key:
        return key, "environment"
    raise AuthError("No BamBuddy API key found", "Run `os2slice setup-keys --bambuddy`")


def store_bambuddy_key(key: str) -> None:
    try:
        keyring.set_password(SERVICE, BAMBUDDY_ENTRY, key)
    except keyring.errors.KeyringError as e:
        raise AuthError(
            f"Couldn't save the BamBuddy key to the system keyring ({type(e).__name__})",
            "Make sure KDE Wallet or GNOME Keyring is running and unlocked",
        ) from e


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
