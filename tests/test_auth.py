"""Module secrets: names, keyring first, then the environment, and the legacy BamBuddy key."""

from __future__ import annotations

import keyring.errors
import pytest

from os2slice import auth
from os2slice.errors import AuthError


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    saved: dict[str, str] = {}
    monkeypatch.setattr("keyring.get_password", lambda service, name: saved.get(name))
    monkeypatch.setattr(
        "keyring.set_password", lambda service, name, value: saved.__setitem__(name, value)
    )
    for env in ("OS2SLICE_SECRET_TARGETS_FARM_API_KEY", auth.ENV_BAMBUDDY,
                "OS2SLICE_SECRET_TARGETS_BAMBUDDY_API_KEY"):  # fmt: skip
        monkeypatch.delenv(env, raising=False)
    return saved


def test_env_name_mapping() -> None:
    assert auth.secret_env("targets.farm.api_key") == "OS2SLICE_SECRET_TARGETS_FARM_API_KEY"
    assert auth.secret_env("slicers.my-orca.token") == "OS2SLICE_SECRET_SLICERS_MY_ORCA_TOKEN"


def test_keyring_then_environment(store: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    assert auth.get_secret("targets.farm.api_key") is None
    monkeypatch.setenv("OS2SLICE_SECRET_TARGETS_FARM_API_KEY", "from-env")
    assert auth.find_secret("targets.farm.api_key") == ("from-env", "environment")
    auth.store_secret("targets.farm.api_key", "from-keyring")
    assert store == {"targets.farm.api_key": "from-keyring"}
    assert auth.find_secret("targets.farm.api_key") == ("from-keyring", "keyring")


def test_legacy_bambuddy_key_is_targets_bambuddy_api_key(
    store: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(auth.ENV_BAMBUDDY, "old-env")
    assert auth.get_secret("targets.bambuddy.api_key") == "old-env"
    store[auth.BAMBUDDY_ENTRY] = "old-keyring"
    assert auth.find_secret("targets.bambuddy.api_key") == ("old-keyring", "keyring")
    assert auth.load_bambuddy_key() == ("old-keyring", "keyring")
    auth.store_bambuddy_key("new")
    assert store["targets.bambuddy.api_key"] == "new"
    assert auth.get_secret("targets.bambuddy.api_key") == "new"
    assert auth.get_secret("targets.farm.api_key") is None  # other targets have no legacy name


def test_bad_names_and_keyring_failures(
    store: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for bad in ("api_key", "targets..x", "targets.farm.api key", "a.b.c.d"):
        with pytest.raises(AuthError, match="Invalid secret name"):
            auth.get_secret(bad)

    def broken(*a: object) -> None:
        raise keyring.errors.KeyringError("locked")

    monkeypatch.setattr("keyring.get_password", broken)
    monkeypatch.setattr("keyring.set_password", broken)
    monkeypatch.setenv("OS2SLICE_SECRET_TARGETS_FARM_API_KEY", "env")
    assert auth.get_secret("targets.farm.api_key") == "env"
    with pytest.raises(AuthError, match="Couldn't save"):
        auth.store_secret("targets.farm.api_key", "x")
