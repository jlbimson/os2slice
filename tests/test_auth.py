"""Secrets: names, keyring → secret file → environment, and the legacy BamBuddy key."""

from __future__ import annotations

import json
import stat
from pathlib import Path

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
    assert not auth.secrets_path().exists()  # a refusing keyring is no reason for a file


# ---- the file store (no keyring: the add-on, the Docker image, a null backend) ----


@pytest.fixture
def no_keyring(store: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """The add-on's setting: the null keyring backend. The keyring must not be touched."""
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.null.Keyring")
    for env in (auth.ENV_ACCESS, auth.ENV_SECRET, auth.ENV_OAUTH_SECRET):
        monkeypatch.delenv(env, raising=False)
    return store


def test_secret_file_round_trips_and_is_private(tmp_path: Path) -> None:
    f = auth.SecretFile(tmp_path / "state" / "secrets.json")
    assert f.read() == {} and f.get("a.b.c") is None
    f.set({"targets.farm.api_key": 'k"1\n'})
    f.set({"access_key": "a"})
    assert f.read() == {"targets.farm.api_key": 'k"1\n', "access_key": "a"}
    assert stat.S_IMODE(f.path.stat().st_mode) == 0o600
    assert f.get("missing", "access_key") == "a" and f.has("access_key")
    assert not f.has("access_key", "secret_key")
    assert f.delete(["access_key", "nope"]) == ["access_key"]
    assert f.read() == {"targets.farm.api_key": 'k"1\n'}
    assert stat.S_IMODE(f.path.stat().st_mode) == 0o600


def test_damaged_secret_file_reads_empty_and_refuses_writes(tmp_path: Path) -> None:
    f = auth.SecretFile(tmp_path / "secrets.json")
    f.path.write_text("[1, 2]")
    assert f.read() == {} and f.get("x") is None
    with pytest.raises(AuthError, match="secret file"):
        f.set({"x": "y"})
    assert f.path.read_text() == "[1, 2]"  # never silently replaced
    assert f.delete(["x"]) == []


@pytest.mark.parametrize(
    "how", ["null-env", "fail-env", "addon", "null-backend", "fail-backend", "broken-backend"]
)
def test_file_store_is_active_without_a_usable_keyring(
    how: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import keyring.backends.fail
    import keyring.backends.null

    assert not auth.file_store_active()  # conftest's fake, usable keyring

    def broken() -> object:
        raise RuntimeError("no dbus")

    if how == "null-env":
        monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.null.Keyring")
    elif how == "fail-env":
        monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    elif how == "addon":
        monkeypatch.setenv("OS2SLICE_ADDON", "1")
    elif how == "null-backend":
        monkeypatch.setattr(auth, "_backend", keyring.backends.null.Keyring)
    elif how == "fail-backend":
        monkeypatch.setattr(auth, "_backend", keyring.backends.fail.Keyring)
    else:
        monkeypatch.setattr(auth, "_backend", broken)
    assert auth.file_store_active()
    assert "secrets.json" in auth.secret_store_name()


def test_null_backend_stores_every_secret_in_the_file(no_keyring: dict[str, str]) -> None:
    auth.store_secret("targets.farm.api_key", "farm")
    auth.store_bambuddy_key("bb")
    auth.store_keys("acc", "sec")
    auth.store_oauth_client_secret("oauth")
    assert no_keyring == {}  # nothing went to the keyring
    path = auth.secrets_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == {
        "targets.farm.api_key": "farm",
        "targets.bambuddy.api_key": "bb",
        "access_key": "acc",
        "secret_key": "sec",
        "oauth_client_secret": "oauth",
    }
    assert auth.find_secret("targets.farm.api_key") == ("farm", "file")
    assert auth.load_bambuddy_key() == ("bb", "file")
    assert auth.load_keys() == auth.Keys("acc", "sec", "file")
    assert auth.load_oauth_client_secret() == "oauth"
    auth.delete_keys()
    with pytest.raises(AuthError, match="No Onshape API keys"):
        auth.load_keys()


def test_file_store_ignores_the_keyring(no_keyring: dict[str, str]) -> None:
    no_keyring["targets.farm.api_key"] = "stale-keyring"
    no_keyring[auth.ACCESS_ENTRY] = no_keyring[auth.SECRET_ENTRY] = "stale"
    assert auth.get_secret("targets.farm.api_key") is None
    with pytest.raises(AuthError):
        auth.load_keys()


def test_order_is_keyring_then_file_then_environment(
    store: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    name = "targets.farm.api_key"
    monkeypatch.setenv(auth.secret_env(name), "env")
    monkeypatch.setenv(auth.ENV_ACCESS, "env-a")
    monkeypatch.setenv(auth.ENV_SECRET, "env-s")
    monkeypatch.setenv(auth.ENV_OAUTH_SECRET, "env-o")
    assert auth.find_secret(name) == ("env", "environment")
    assert auth.load_keys().source == "environment"
    assert auth.load_oauth_client_secret() == "env-o"
    auth.SecretFile(auth.secrets_path()).set(
        {
            name: "file",
            "access_key": "file-a",
            "secret_key": "file-s",
            "oauth_client_secret": "file-o",
        }
    )
    assert auth.find_secret(name) == ("file", "file")
    assert auth.load_keys() == auth.Keys("file-a", "file-s", "file")
    assert auth.load_oauth_client_secret() == "file-o"
    auth.store_secret(name, "keyring")  # a usable keyring: stored there, not in the file
    store[auth.OAUTH_SECRET_ENTRY] = "keyring-o"
    assert auth.find_secret(name) == ("keyring", "keyring")
    assert auth.load_oauth_client_secret() == "keyring-o"
    assert json.loads(auth.secrets_path().read_text())[name] == "file"


def test_key_pair_comes_whole_from_one_place(
    no_keyring: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    auth.SecretFile(auth.secrets_path()).set({"access_key": "file-a"})  # half a pair
    monkeypatch.setenv(auth.ENV_ACCESS, "env-a")
    monkeypatch.setenv(auth.ENV_SECRET, "env-s")
    assert auth.load_keys() == auth.Keys("env-a", "env-s", "environment")


def test_legacy_names_and_environment_work_with_the_file_store(
    no_keyring: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(auth.ENV_BAMBUDDY, "legacy-env")
    assert auth.load_bambuddy_key() == ("legacy-env", "environment")
    monkeypatch.setenv("OS2SLICE_SECRET_TARGETS_FARM_API_KEY", "farm-env")
    assert auth.get_secret("targets.farm.api_key") == "farm-env"
    # The legacy entry name is read from the file too.
    auth.SecretFile(auth.secrets_path()).set({auth.BAMBUDDY_ENTRY: "legacy-file"})
    assert auth.load_bambuddy_key() == ("legacy-file", "file")
    auth.store_bambuddy_key("new")
    assert auth.load_bambuddy_key() == ("new", "file")
