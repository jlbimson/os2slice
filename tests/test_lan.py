"""LAN mode (D-17): HTTPS with a certificate from disk, open access, strict Host."""

from __future__ import annotations

import dataclasses
import os
import shutil
import subprocess
import threading
from pathlib import Path

import httpx
import pytest

from os2slice import config, server
from os2slice.auth import Keys
from os2slice.bambuddy import BambuddyClient
from os2slice.config import Config, ServerConfig
from os2slice.errors import ConfigError
from os2slice.onshape import OnshapeClient
from tests.fakes import FakeBambuddy, fake_onshape

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")
NAME = "davis-print.duckdns.org"


def make_cert(tmp: Path, cn: str = NAME) -> tuple[Path, Path]:
    cert, key = tmp / f"{cn}.pem", tmp / f"{cn}.key"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
         "-nodes", "-days", "2", "-subj", f"/CN={cn}", "-addext", f"subjectAltName=DNS:{cn}",
         "-keyout", str(key), "-out", str(cert)],
        check=True, capture_output=True,
    )  # fmt: skip
    return cert, key


@pytest.fixture
def lan(cfg: Config, tmp_path: Path):
    cert, key = make_cert(tmp_path)
    lan_cfg = dataclasses.replace(
        cfg,
        server=ServerConfig(
            bind="127.0.0.1", hosts=(f"{NAME}:8443",), identity="lan", tls_cert=cert, tls_key=key
        ),
    )
    svc = server.Service(
        lan_cfg,
        onshape=lambda: OnshapeClient(
            "https://cad.onshape.com", Keys("a", "b"), transport=httpx.MockTransport(fake_onshape)
        ),
        bambuddy=lambda: BambuddyClient(
            "http://bb.test", "k", transport=httpx.MockTransport(FakeBambuddy())
        ),
    )
    httpd = server.make_server(svc, port=0)
    threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True).start()
    yield httpd, cert
    httpd.shutdown()


def test_https_health_and_host_check(lan) -> None:
    httpd, _cert = lan
    url = f"https://127.0.0.1:{httpd.server_port}/health"
    ok = httpx.get(url, headers={"Host": f"{NAME}:8443"}, verify=False)
    assert ok.status_code == 200 and ok.json()["ok"]
    assert httpx.get(url, headers={"Host": "evil.example"}, verify=False).status_code == 403
    with pytest.raises(httpx.HTTPError):
        httpx.get(url.replace("https", "http"), headers={"Host": f"{NAME}:8443"})


def test_certificate_reload(tmp_path: Path) -> None:
    cert, key = make_cert(tmp_path)
    tls = server.TlsCertificate(cert, key)
    assert tls.reload_if_changed() is False
    new_cert, new_key = make_cert(tmp_path / "..", cn=NAME)  # a renewed pair
    shutil.copy(new_cert, cert)
    shutil.copy(new_key, key)
    st = cert.stat()
    os.utime(cert, (st.st_atime, st.st_mtime + 10))
    assert tls.reload_if_changed() is True


def _parse(server_table: dict) -> Config:
    return config.parse({"server": server_table}, Path("/x/config.toml"))


def test_lan_config_rules(tmp_path: Path) -> None:
    ok = _parse({"identity": "lan", "bind": "0.0.0.0", "port": 8443, "hosts": [f"{NAME}:8443"],
                 "tls_cert": "/ssl/fullchain.pem", "tls_key": "/ssl/privkey.pem"})  # fmt: skip
    assert ok.server.identity == "lan" and ok.server.tls_cert == Path("/ssl/fullchain.pem")
    bad = [
        {"identity": "lan", "hosts": [NAME]},  # no TLS
        {"identity": "lan", "tls_cert": "/a", "tls_key": "/b"},  # no explicit hosts
        {"identity": "none", "bind": "0.0.0.0"},  # open bind without lan mode
        {"identity": "lan", "hosts": [NAME], "tls_cert": "/a"},  # key missing
        {"bind": "not-an-ip"},
    ]
    for table in bad:
        with pytest.raises(ConfigError):
            _parse(table)
