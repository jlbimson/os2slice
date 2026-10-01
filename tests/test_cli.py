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
