"""The plain-HTTP listener that only redirects to the HTTPS /admin page."""

from __future__ import annotations

import http.client
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from os2slice import config
from os2slice.errors import ConfigError
from os2slice.redirect import make_redirect_server

NAME = "print.example.duckdns.org"
TARGET = f"https://{NAME}:8443/admin"
LAN = {"identity": "lan", "bind": "0.0.0.0", "port": 8443, "hosts": [f"{NAME}:8443"],
       "tls_cert": "/ssl/fullchain.pem", "tls_key": "/ssl/privkey.pem"}  # fmt: skip


def _parse(server_table: dict[str, Any]) -> config.Config:
    return config.parse({"server": server_table}, Path("/x/config.toml"))


def test_redirect_port_is_off_by_default() -> None:
    assert _parse({}).server.redirect_port == 0
    assert _parse(LAN).server.redirect_port == 0
    assert _parse({**LAN, "redirect_port": 8080}).server.redirect_port == 8080


@pytest.mark.parametrize(
    ("table", "message"),
    [
        ({"redirect_port": 8080}, 'needs identity = "lan"'),
        ({"identity": "tailscale", "allowed_users": ["a@b"], "redirect_port": 8080}, '"lan"'),
        ({**LAN, "redirect_port": 8443}, "must differ"),
        ({**LAN, "redirect_port": 70000}, "redirect_port must be"),
        ({**LAN, "redirect_port": -1}, "redirect_port must be"),
        ({**LAN, "redirect_port": "8080"}, "redirect_port must be"),
        ({**LAN, "redirect_port": True}, "redirect_port must be"),
    ],
)
def test_redirect_port_refusals(table: dict[str, Any], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        _parse(table)


@pytest.fixture
def port() -> Iterator[int]:
    httpd = make_redirect_server("127.0.0.1", 0, TARGET)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def _request(port: int, method: str, path: str = "/", **kw: Any) -> http.client.HTTPResponse:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, **kw)
    return conn.getresponse()


def test_get_any_path_redirects(port: int) -> None:
    for path in ("/", "/anything?x=<b>", "/admin", "//evil.example/x"):
        r = _request(port, "GET", path, headers={"Host": "evil.example"})
        body = r.read()
        assert r.status == 303
        assert r.getheader("Location") == TARGET
        assert r.getheader("Cache-Control") == "no-store"
        assert r.getheader("Content-Type", "").startswith("text/plain")
        assert body and b"evil" not in body and b"<b>" not in body


def test_head_redirects_without_a_body(port: int) -> None:
    r = _request(port, "HEAD", "/x")
    assert r.status == 303 and r.getheader("Location") == TARGET
    assert r.read() == b""


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "OPTIONS", "MADEUP"])
def test_other_methods_are_refused(port: int, method: str) -> None:
    r = _request(port, method, "/admin", body=b"password=x")
    assert r.status == 405 and r.getheader("Location") is None
    r.read()
