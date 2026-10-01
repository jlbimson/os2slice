from __future__ import annotations

import base64
from collections.abc import Callable

import httpx
import pytest

from os2slice.auth import Keys
from os2slice.errors import AuthError, OnshapeError
from os2slice.onshape import OnshapeClient, check_binary_stl
from os2slice.request import parse_query
from tests.conftest import DOC, ELEM, QUERY_NO_CONFIG, QUERY_WITH_CONFIG, SLICERS, WS, make_stl

KEYS = Keys("fake-access", "fake-secret", "test")
AUTH = "Basic " + base64.b64encode(b"fake-access:fake-secret").decode()
STL_PATH = f"/api/partstudios/d/{DOC}/w/{WS}/e/{ELEM}/stl"
DOWNLOAD = "https://cad-usw2.onshape.com/modelexport?format=STL&partIds=JHD"


def client(handler: Callable[[httpx.Request], httpx.Response]) -> OnshapeClient:
    return OnshapeClient("https://cad.onshape.com", KEYS, transport=httpx.MockTransport(handler))


def export_handler(seen: list[httpx.Request], location: str = DOWNLOAD, body: bytes | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == STL_PATH:
            return httpx.Response(307, headers={"Location": location})
        return httpx.Response(200, content=make_stl() if body is None else body)

    return handler


def test_export_follows_redirect_with_auth() -> None:
    seen: list[httpx.Request] = []
    req = parse_query(QUERY_NO_CONFIG, SLICERS)
    data = client(export_handler(seen)).export_stl(req)
    assert check_binary_stl(data) == 12
    first, second = seen
    assert first.url.host == "cad.onshape.com"
    assert first.url.params["partIds"] == "JHD"
    assert first.url.params["units"] == "millimeter"
    assert "configuration" not in first.url.params
    assert second.url.host == "cad-usw2.onshape.com"
    assert second.url.params["format"] == "STL"  # the Location's query is used as-is
    assert first.headers["Authorization"] == second.headers["Authorization"] == AUTH


def test_configuration_is_encoded_exactly_once() -> None:
    seen: list[httpx.Request] = []
    req = parse_query(QUERY_WITH_CONFIG, SLICERS)
    client(export_handler(seen)).export_stl(req)
    assert b"configuration=List_zrSB7lcyzQWXqq%3D_1" in seen[0].url.query


@pytest.mark.parametrize(
    "location",
    [
        "https://evil.example/steal",
        "http://cad-usw2.onshape.com/modelexport",
        "https://cad.onshape.com.evil.example/x",
        "https://cad-usw2.onshape.com:8443/modelexport",
        "https://onshape.com.evil/x",
    ],
)
def test_redirect_to_non_onshape_host_is_refused(location: str) -> None:
    seen: list[httpx.Request] = []
    req = parse_query(QUERY_NO_CONFIG, SLICERS)
    with pytest.raises(OnshapeError, match="Refusing to follow a redirect"):
        client(export_handler(seen, location)).export_stl(req)
    assert len(seen) == 1  # nothing (and no auth) was sent to the other host


def test_redirect_loop_is_capped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(307, headers={"Location": "https://cad.onshape.com/again"})

    with pytest.raises(OnshapeError, match="Too many redirects"):
        client(handler).get_document_name(DOC)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"<html>oops</html>", "too small"),
        (b"\0" * 80 + (5).to_bytes(4, "little") + b"x" * 10, "isn't a binary STL"),
        (b"\0" * 84, "empty"),
    ],
)
def test_bad_stl_bodies(body: bytes, message: str) -> None:
    req = parse_query(QUERY_NO_CONFIG, SLICERS)
    with pytest.raises(OnshapeError, match=message):
        client(export_handler([], body=body)).export_stl(req)


@pytest.mark.parametrize(
    ("status", "error", "message"),
    [
        (401, AuthError, "rejected the API keys"),
        (403, AuthError, "No access"),
        (404, OnshapeError, "couldn't find"),
        (429, OnshapeError, "rate limit"),
        (500, OnshapeError, "API error 500"),
    ],
)
def test_http_errors(status: int, error: type[Exception], message: str) -> None:
    c = client(lambda r: httpx.Response(status, json={"message": "Not found.", "status": status}))
    with pytest.raises(error, match=message):
        c.get_document_name(DOC)


def test_network_errors() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    with pytest.raises(OnshapeError, match="Can't reach Onshape"):
        client(boom).get_document_name(DOC)


def test_check_keys_treats_204_as_bad_keys() -> None:
    with pytest.raises(AuthError, match="didn't accept"):
        client(lambda r: httpx.Response(204)).check_keys()
    ok = client(lambda r: httpx.Response(200, json={"email": "me@example.com"}))
    assert ok.check_keys() == "me@example.com"


def test_names() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/documents/{DOC}":
            return httpx.Response(200, json={"name": "test"})
        assert request.url.path == f"/api/parts/d/{DOC}/w/{WS}/e/{ELEM}"
        assert request.url.params["configuration"] == "List_zrSB7lcyzQWXqq=_1"
        return httpx.Response(
            200, json=[{"partId": "JHD", "name": "Part 1"}, {"partId": "JKD", "name": "Other"}]
        )

    c = client(handler)
    req = parse_query(QUERY_WITH_CONFIG, SLICERS)
    assert c.get_document_name(DOC) == "test"
    assert c.get_part_name(req) == "Part 1"
    assert (
        c.get_part_name(parse_query(QUERY_WITH_CONFIG.replace("p=JHD", "p=ZZZ"), SLICERS)) == "ZZZ"
    )
