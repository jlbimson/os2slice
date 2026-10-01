from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from os2slice import pipeline
from os2slice.auth import Keys
from os2slice.config import Config, SlicerConfig
from os2slice.errors import SlicerError
from os2slice.onshape import OnshapeClient
from os2slice.request import parse_query
from tests.conftest import DOC, QUERY_WITH_CONFIG, SLICERS, make_stl

KEYS = Keys("a", "b", "test")


def fake_onshape(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == f"/api/documents/{DOC}":
        return httpx.Response(200, json={"name": "Test/Doc"})
    if path.startswith("/api/parts/"):
        return httpx.Response(200, json=[{"partId": "JHD", "name": "Part 1"}])
    if path.endswith("/stl"):
        return httpx.Response(307, headers={"Location": "https://cad-usw2.onshape.com/modelexport"})
    if path == "/modelexport":
        return httpx.Response(200, content=make_stl())
    return httpx.Response(404)


def onshape() -> OnshapeClient:
    return OnshapeClient(
        "https://cad.onshape.com", KEYS, transport=httpx.MockTransport(fake_onshape)
    )


def test_happy_path(cfg: Config, notifications: list) -> None:
    launched: list[tuple[str, Path]] = []
    result = pipeline.run_and_report(
        lambda: parse_query(QUERY_WITH_CONFIG, SLICERS),
        cfg,
        client=onshape(),
        launcher=lambda s, f, log: launched.append((s.key, f)),
    )
    assert result.ok, result
    assert result.title == "Sent Part 1 to OrcaSlicer"
    assert result.path is not None and result.path.parent == cfg.export_dir / "Test_Doc"
    assert result.path.name.startswith("Part 1_") and result.path.suffix == ".stl"
    assert result.path.read_bytes() == make_stl()
    assert launched == [("orca", result.path)]
    assert notifications == [(result.title, result.detail, False)]


def test_bad_request_is_reported(cfg: Config, notifications: list) -> None:
    result = pipeline.run_and_report(
        lambda: parse_query("slicer=orca", SLICERS), cfg, client=onshape()
    )
    assert (result.ok, result.exit_code, result.http_status) == (False, 2, 400)
    assert notifications[0][2] is True and "Missing parameter" in notifications[0][0]


def test_missing_keys_are_reported(cfg: Config, notifications: list) -> None:
    result = pipeline.run_and_report(lambda: parse_query(QUERY_WITH_CONFIG, SLICERS), cfg)
    assert (result.ok, result.exit_code) == (False, 3)
    assert "setup-keys" in result.detail


def test_slicer_failure_is_reported(cfg: Config) -> None:
    def fail(s: SlicerConfig, f: Path, log: Path) -> None:
        raise SlicerError("OrcaSlicer not found: orca-slicer", "Fix it")

    result = pipeline.run_and_report(
        lambda: parse_query(QUERY_WITH_CONFIG, SLICERS), cfg, client=onshape(), launcher=fail
    )
    assert (result.ok, result.exit_code) == (False, 5)


def test_unexpected_error_is_reported(cfg: Config, notifications: list) -> None:
    def crash() -> None:
        raise RuntimeError("bug")

    result = pipeline.run_and_report(crash, cfg)  # type: ignore[arg-type]
    assert (result.ok, result.exit_code) == (False, 1)
    assert "unexpected" in result.title and notifications


@pytest.mark.parametrize(
    ("extra", "message"),
    [("&fmt=3mf", "3MF export isn't implemented"), ("", "Whole Part Studio")],
)
def test_not_yet_implemented(cfg: Config, extra: str, message: str) -> None:
    query = QUERY_WITH_CONFIG + extra
    if not extra:
        query = query.replace("&p=JHD", "")
    result = pipeline.run_and_report(lambda: parse_query(query, SLICERS), cfg, client=onshape())
    assert not result.ok and message in result.title
