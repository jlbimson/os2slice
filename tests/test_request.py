from __future__ import annotations

import pytest

from os2slice.errors import BadRequest
from os2slice.request import (
    ExportRequest,
    parse_listener_url,
    parse_onshape_url,
    parse_query,
)
from tests.conftest import (
    DOC,
    ELEM,
    ONSHAPE_EXTRAS,
    QUERY_NO_CONFIG,
    QUERY_WITH_CONFIG,
    SLICERS,
    WS,
)

BASE = f"slicer=orca&d={DOC}&wv=w&wvid={WS}&e={ELEM}&p=JHD"


def test_real_onshape_query_without_configuration() -> None:
    req = parse_query(QUERY_NO_CONFIG, SLICERS)
    assert req == ExportRequest(
        slicer="orca",
        document_id=DOC,
        wvm="w",
        wvm_id=WS,
        element_id=ELEM,
        part_id="JHD",
        configuration="",
        fmt="stl",
    )


def test_real_onshape_query_with_configuration_is_decoded_once() -> None:
    req = parse_query(QUERY_WITH_CONFIG, SLICERS)
    assert req.configuration == "List_zrSB7lcyzQWXqq=_1"


@pytest.mark.parametrize(
    ("encoded", "decoded"),
    [
        ("Length%3D50%2Bmm", "Length=50+mm"),
        ("Length%3D50+mm", "Length=50 mm"),  # form encoding: + is a space
        ("A%3D1%3BB%3Dtrue", "A=1;B=true"),
        ("Name%3D%C3%A9t%C3%A9", "Name=été"),
    ],
)
def test_configuration_encodings(encoded: str, decoded: str) -> None:
    assert parse_query(f"{BASE}&c={encoded}", SLICERS).configuration == decoded


def test_minimal_query_and_version_workspace() -> None:
    req = parse_query(f"slicer=bambu&d={DOC}&wv=v&wvid={WS}&e={ELEM}", SLICERS)
    assert (req.slicer, req.wvm, req.part_id, req.configuration) == ("bambu", "v", None, "")


def test_default_format_comes_from_config() -> None:
    assert parse_query(BASE, SLICERS, default_fmt="3mf").fmt == "3mf"
    assert parse_query(BASE + "&fmt=step", SLICERS).fmt == "step"


@pytest.mark.parametrize(
    ("query", "message"),
    [
        (BASE.replace("slicer=orca", "slicer=cura"), "isn't configured"),
        (BASE.replace("slicer=orca", "slicer=ORCA"), "Invalid slicer"),
        (BASE.replace("slicer=orca", "slicer=..%2Fetc"), "Invalid slicer"),
        (BASE.replace(f"d={DOC}", "d=123"), "Invalid d"),
        (BASE.replace(f"d={DOC}", "d=" + DOC.upper()), "Invalid d"),
        (BASE.replace(f"wvid={WS}", "wvid=" + WS + "0"), "Invalid wvid"),
        (BASE.replace(f"e={ELEM}", "e=" + "g" * 24), "Invalid e"),
        (BASE.replace("wv=w", "wv=x"), "Invalid wv"),
        (BASE.replace("p=JHD", "p=JH%2FD"), "Invalid part"),
        (BASE.replace("p=JHD", "p=" + "A" * 33), "Invalid part"),
        (BASE.replace("p=JHD", "p=%7B$partId%7D"), "unresolved Onshape placeholder"),
        (BASE + "&fmt=obj", "Invalid fmt"),
        (BASE + "&c=" + "x" * 2049, "longer than"),
        (BASE + "&c=a%0Ab", "control characters"),
        (BASE + "&c=%7B$foo%7D", "unresolved Onshape placeholder"),
        (BASE.replace(f"&e={ELEM}", ""), "Missing parameter(s): e"),
        ("", "Missing parameter"),
    ],
)
def test_bad_fields(query: str, message: str) -> None:
    with pytest.raises(BadRequest, match=message.replace("(", r"\(").replace(")", r"\)")):
        parse_query(query, SLICERS)


@pytest.mark.parametrize(
    "extra",
    ["&host=evil.example", "&P=JHD", "&url=https://evil.example", "&cmd=rm"],
)
def test_unknown_params_are_rejected(extra: str) -> None:
    with pytest.raises(BadRequest, match="Unknown parameter"):
        parse_query(BASE + extra, SLICERS)


def test_repeated_params_are_rejected() -> None:
    with pytest.raises(BadRequest, match="more than once"):
        parse_query(BASE + f"&d={DOC}", SLICERS)


def test_onshape_extras_are_allowed_but_server_is_ignored() -> None:
    evil = ONSHAPE_EXTRAS.replace("cad.onshape.com", "evil.example")
    req = parse_query(BASE + evil, SLICERS)
    assert "evil" not in repr(req)


def test_too_many_fields_and_malformed_query() -> None:
    with pytest.raises(BadRequest):
        parse_query(BASE + "&theme=x" * 40, SLICERS)
    with pytest.raises(BadRequest, match="Malformed"):
        parse_query(BASE + "&&junk", SLICERS)


def test_preform_on_linux_has_a_clear_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    with pytest.raises(BadRequest, match="PreForm isn't supported on Linux"):
        parse_query(BASE.replace("slicer=orca", "slicer=preform"), SLICERS)


@pytest.mark.parametrize(
    "url",
    [
        f"http://localhost:8765/open?{QUERY_WITH_CONFIG}",
        f"http://127.0.0.1:8765/open?{QUERY_WITH_CONFIG}",
        f"os2slice://open?{QUERY_WITH_CONFIG}",
        f"os2slice:open?{QUERY_WITH_CONFIG}",
    ],
)
def test_listener_urls(url: str) -> None:
    assert parse_listener_url(url, SLICERS).part_id == "JHD"


@pytest.mark.parametrize(
    "url",
    [
        f"https://localhost:8765/open?{BASE}",
        f"http://evil.example/open?{BASE}",
        f"http://localhost:8765/close?{BASE}",
        f"http://localhost.evil.example:8765/open?{BASE}",
        f"os2slice://test?{BASE}",
        f"os2slice://open/extra?{BASE}",
        f"http://localhost:8765/open?{BASE}#frag",
        f"file:///open?{BASE}",
    ],
)
def test_bad_listener_urls(url: str) -> None:
    with pytest.raises(BadRequest):
        parse_listener_url(url, SLICERS)


def test_onshape_browser_url() -> None:
    url = f"https://cad.onshape.com/documents/{DOC}/w/{WS}/e/{ELEM}"
    req = parse_onshape_url(url, "prusa", SLICERS, "JHD")
    assert (req.document_id, req.wvm_id, req.element_id, req.part_id) == (DOC, WS, ELEM, "JHD")


def test_onshape_browser_url_keeps_its_configuration() -> None:
    url = f"https://acme.onshape.com/documents/{DOC}/v/{WS}/e/{ELEM}?configuration=A%3D1"
    assert parse_onshape_url(url, "orca", SLICERS, None).configuration == "A=1"
    assert parse_onshape_url(url, "orca", SLICERS, None, "B=2").configuration == "B=2"


@pytest.mark.parametrize(
    "url",
    [
        f"http://cad.onshape.com/documents/{DOC}/w/{WS}/e/{ELEM}",
        f"https://cad.onshape.com.evil.example/documents/{DOC}/w/{WS}/e/{ELEM}",
        f"https://evil.example/documents/{DOC}/w/{WS}/e/{ELEM}",
        f"https://cad.onshape.com/documents/{DOC}/w/{WS}",
        f"https://cad.onshape.com/documents/{DOC}/w/{WS}/e/{ELEM}/extra",
    ],
)
def test_bad_onshape_browser_urls(url: str) -> None:
    with pytest.raises(BadRequest):
        parse_onshape_url(url, "orca", SLICERS, "JHD")
