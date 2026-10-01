"""Live Onshape test. Run with: OS2SLICE_LIVE_URL='<part studio url>' pytest -q -m live

Needs real keys in the keyring (or $ONSHAPE_ACCESS_KEY / $ONSHAPE_SECRET_KEY).
Optional: OS2SLICE_LIVE_PART (defaults to the first part).
"""

from __future__ import annotations

import os

import keyring
import pytest

from os2slice import auth
from os2slice.onshape import OnshapeClient, check_binary_stl
from os2slice.request import parse_onshape_url

pytestmark = pytest.mark.live
LIVE_URL = os.environ.get("OS2SLICE_LIVE_URL", "")


@pytest.fixture(autouse=True)
def real_keys(monkeypatch: pytest.MonkeyPatch) -> auth.Keys:
    if not LIVE_URL:
        pytest.skip("OS2SLICE_LIVE_URL not set")
    monkeypatch.undo()  # the global fixture stubs out the keyring and env
    return auth.load_keys()


def test_live_export(real_keys: auth.Keys) -> None:
    req = parse_onshape_url(LIVE_URL, "live", ["live"], None)
    with OnshapeClient("https://cad.onshape.com", real_keys) as client:
        client.check_keys()  # raises if the keys are refused
        parts = client.list_parts(req)
        assert parts, "the Part Studio has no parts"
        part_id = os.environ.get("OS2SLICE_LIVE_PART") or parts[0]["partId"]
        req = parse_onshape_url(LIVE_URL, "live", ["live"], part_id)
        assert client.get_document_name(req.document_id)
        assert client.get_part_name(req) != part_id
        assert check_binary_stl(client.export_stl(req)) > 0


def test_keyring_is_reachable() -> None:
    keyring.get_keyring()


def test_live_bambuddy_slice_only(real_keys: auth.Keys) -> None:
    """Export → orient → upload → slice in the real BamBuddy. Never queues a print."""
    from os2slice import config, printing
    from os2slice.bambuddy import BambuddyClient
    from os2slice.orientation import Orientation
    from os2slice.settings import PrintSettings

    cfg = config.load(create=False)
    if cfg.bambuddy is None:
        pytest.skip("no [bambuddy] in config")
    key, _ = auth.load_bambuddy_key()
    with (
        OnshapeClient(cfg.onshape_base_url, real_keys) as onshape,
        BambuddyClient(cfg.bambuddy.base_url, key) as bb,
    ):
        req = parse_onshape_url(LIVE_URL, "bambuddy", ["bambuddy"], None)
        part_id = os.environ.get("OS2SLICE_LIVE_PART") or onshape.list_parts(req)[0]["partId"]
        req = parse_onshape_url(LIVE_URL, "bambuddy", ["bambuddy"], part_id)
        plan = printing.plan_print(
            req, cfg, onshape, bb, None, Orientation.parse("x+"), PrintSettings(3, 20, "tree")
        )
        out = printing.execute_print(plan, cfg, onshape, bb, queue=False)
    assert not out.queued
    assert out.slice.library_file_id > 0
