"""The Docker Compose files stay in step with the code (docs/DOCKER.md)."""

from __future__ import annotations

import re
from pathlib import Path

from os2slice import auth, config

ROOT = Path(__file__).resolve().parent.parent


def test_example_config_is_valid_and_matches_the_compose_mounts() -> None:
    cfg = config.load(ROOT / "docker/config.example.toml", create=False)
    assert cfg.server.identity == "lan" and cfg.server.bind == "0.0.0.0"
    assert cfg.server.port == 8443 and cfg.onshape_auth == "oauth"
    compose = (ROOT / "docker-compose.yml").read_text()
    # XDG_CONFIG_HOME=/data in the image, so the config lives at /data/os2slice/config.toml.
    assert "./docker/config.toml:/data/os2slice/config.toml:ro" in compose
    assert "XDG_CONFIG_HOME=/data" in (ROOT / "Dockerfile").read_text()
    assert str(cfg.server.tls_cert).startswith("/certs/") and "./certs:/certs:ro" in compose
    assert cfg.web_studio is not None and str(cfg.web_studio.inbox).startswith("/share/os2slice")
    assert '"8443:8443"' in compose


def test_env_example_names_the_variables_the_code_reads() -> None:
    names = set(re.findall(r"^#?([A-Z_]+)=", (ROOT / "docker/env.example").read_text(), re.M))
    for var in (auth.ENV_OAUTH_SECRET, auth.ENV_BAMBUDDY, auth.ENV_ACCESS, auth.ENV_SECRET):
        assert var in names
    script = (ROOT / "docker/duckdns-certs.sh").read_text()
    for var in re.findall(r"\$\{([A-Z_]+):\?", script):
        assert var in names


def test_local_settings_and_secrets_are_ignored() -> None:
    ignore = (ROOT / ".gitignore").read_text()
    for pattern in (".env*", "/docker/config.toml", "/certs/"):
        assert pattern in ignore.splitlines()
