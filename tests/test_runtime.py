"""Where os2slice runs (runtime.py), and the messages that depend on it."""

from __future__ import annotations

import pytest

from os2slice import auth
from os2slice.errors import AuthError
from os2slice.runtime import runtime


@pytest.mark.parametrize(
    ("addon", "value", "expected"),
    [
        ("", "", "desktop"),
        ("1", "", "home-assistant"),  # an add-on started by an older launcher
        ("1", "home-assistant", "home-assistant"),
        ("1", "docker", "docker"),
        ("", "something else", "desktop"),
    ],
)
def test_runtime(monkeypatch: pytest.MonkeyPatch, addon: str, value: str, expected: str) -> None:
    monkeypatch.setenv("OS2SLICE_ADDON", addon)
    monkeypatch.setenv("OS2SLICE_RUNTIME", value)
    assert runtime() == expected


@pytest.mark.parametrize(
    ("value", "fix"),
    [
        ("docker", "Set ONSHAPE_OAUTH_CLIENT_SECRET in .env, then run docker compose up -d"),
        ("home-assistant", "Set onshape_oauth_client_secret on the add-on's Configuration tab"),
    ],
)
def test_missing_oauth_secret_says_where_to_set_it(
    monkeypatch: pytest.MonkeyPatch, value: str, fix: str
) -> None:
    monkeypatch.setenv("OS2SLICE_ADDON", "1")
    monkeypatch.setenv("OS2SLICE_RUNTIME", value)
    monkeypatch.delenv(auth.ENV_OAUTH_SECRET, raising=False)
    monkeypatch.setattr(auth, "_keyring_get", lambda name: None)
    monkeypatch.setattr(auth, "_file", lambda: {})
    with pytest.raises(AuthError) as e:
        auth.load_oauth_client_secret()
    assert e.value.fix == fix
