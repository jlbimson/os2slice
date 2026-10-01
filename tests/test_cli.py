from __future__ import annotations

import pytest

from os2slice import cli


def test_handle_bad_url_exits_2_and_notifies(notifications: list) -> None:
    assert cli.main(["handle", "http://evil.example/open?x=1"]) == 2
    assert notifications and notifications[0][2] is True


def test_handle_without_keys_exits_3(notifications: list) -> None:
    from tests.conftest import QUERY_WITH_CONFIG

    assert cli.main(["handle", f"http://localhost:8765/open?{QUERY_WITH_CONFIG}"]) == 3
    assert "setup-keys" in notifications[0][1]


def test_setup_keys_from_env_requires_keys(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["setup-keys", "--from-env"]) == 2
    assert "Both keys are needed" in capsys.readouterr().err


def test_doctor_offline_reports_missing_keys(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["doctor", "--offline"]) == 1
    out = capsys.readouterr().out
    assert "FAIL  API keys" in out and "PASS  config" in out
    assert "FAIL  targets.bambuddy.api_key" in out and "setup-keys --bambuddy" in out


def test_doctor_checks_every_module(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import functools

    import httpx

    from os2slice import auth
    from os2slice.modules import registry
    from tests.fakes import FakeBambuddy

    monkeypatch.setenv(auth.ENV_BAMBUDDY, "bb-key")  # the legacy variable still works
    fake = FakeBambuddy()
    real = registry.Modules.from_config.__func__  # type: ignore[attr-defined]
    monkeypatch.setattr(
        registry.Modules,
        "from_config",
        classmethod(functools.partial(real, transport=httpx.MockTransport(fake))),
    )
    cli.main(["doctor"])
    out = capsys.readouterr().out
    assert "WARN  targets.bambuddy.api_key" in out  # from the environment
    assert "PASS  slicer + target bambuddy" in out and "4 printers" in out
    # The default config has presets for A1 Mini, X1C and H2D; the fake's P1S is inactive.
    assert "WARN  slicer + target bambuddy" not in out
    assert "bb-key" not in out
