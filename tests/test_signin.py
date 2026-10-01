"""Per-user Onshape sign-in (D-23): token handling and the browser flow, with fakes only."""

from __future__ import annotations

import dataclasses
import json
import re
import stat
import threading
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from os2slice import server
from os2slice.config import Config
from os2slice.errors import AuthError
from os2slice.modules.registry import Modules
from os2slice.oauth import (
    BearerAuth,
    Grant,
    GrantStore,
    OAuthSettings,
    TokenEndpoint,
    UserTokens,
)
from os2slice.onshape import OnshapeClient
from tests.conftest import DOC, ELEM, WS
from tests.fakes import FakeBambuddy, fake_modules, fake_onshape

HOST = "localhost:8765"
NAV = {"Host": HOST, "Sec-Fetch-Dest": "document", "Sec-Fetch-Site": "cross-site"}
SAME = {"Host": HOST, "Sec-Fetch-Site": "same-origin", "Origin": f"http://{HOST}"}
FRAME = {"Host": HOST, "Sec-Fetch-Dest": "iframe", "Sec-Fetch-Site": "cross-site"}
PANEL = f"/panel?d={DOC}&wv=w&wvid={WS}&e={ELEM}"
USERS = {"AT-A": ("a" * 24, "Pat"), "AT-A2": ("a" * 24, "Pat"), "AT-B": ("b" * 24, "Sam")}
SETTINGS = OAuthSettings("CLIENTID123", "client-secret", f"https://{HOST}/auth/callback")


class FakeOAuth:
    """The token endpoint: two users' codes, and one refresh that rotates the token."""

    def __init__(self) -> None:
        self.forms: list[dict[str, str]] = []
        self.refuse_refresh = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url == "https://oauth.onshape.com/oauth/token"
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.forms.append(form)
        assert form["client_id"] == "CLIENTID123" and form["client_secret"] == "client-secret"
        if form["grant_type"] == "authorization_code":
            assert form["redirect_uri"] == SETTINGS.redirect_uri
            user = {"code-A": "A", "code-B": "B"}.get(form["code"])
            if user is None:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(
                200,
                json={
                    "access_token": f"AT-{user}",
                    "refresh_token": f"RT-{user}",
                    "expires_in": 3600,
                },
            )
        refresh = form["grant_type"] == "refresh_token" and not self.refuse_refresh
        if refresh and form["refresh_token"] == "RT-A":
            return httpx.Response(
                200,
                json={"access_token": "AT-A2", "refresh_token": "RT-A2", "expires_in": 3600},
            )
        return httpx.Response(400, json={"error": "invalid_grant"})


def onshape_with_bearer(seen: list[str]):
    """fake_onshape behind bearer auth; user B can't read the part's mesh."""

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ")
        seen.append(token)
        if not auth.startswith("Bearer ") or token not in USERS:
            return httpx.Response(401)
        if request.url.path == "/api/users/sessioninfo":
            uid, name = USERS[token]
            return httpx.Response(200, json={"id": uid, "name": name})
        if token == "AT-B" and request.url.path.endswith("/stl"):
            return httpx.Response(403, json={"message": "No access"})
        return fake_onshape(request)

    return handler


# -- tokens ----------------------------------------------------------------------


def test_grant_store_is_private_and_keeps_only_session_hashes(tmp_path: Path) -> None:
    path = tmp_path / "signins.json"
    store = GrantStore(path)
    store.put_grant(Grant("a" * 24, "Pat", "AT-A", "RT-A", 9e9))
    sid = store.new_session("a" * 24)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert sid not in path.read_text()  # only its hash is stored
    again = GrantStore(path)  # survives a restart
    assert again.session_user(sid).name == "Pat"  # type: ignore[union-attr]
    assert again.session_user("forged") is None
    assert again.session_user(sid, now=9e12) is None  # expired
    again.end_session(sid)
    assert GrantStore(path).session_user(sid) is None


def test_tokens_refresh_before_expiry_and_rotate(tmp_path: Path) -> None:
    oauth = FakeOAuth()
    store = GrantStore(tmp_path / "s.json")
    now = [1000.0]
    tokens = UserTokens(
        store, TokenEndpoint(SETTINGS, httpx.MockTransport(oauth)), clock=lambda: now[0]
    )
    tokens.save_new(
        "a" * 24, "Pat", {"access_token": "AT-A", "refresh_token": "RT-A", "expires_in": 3600}
    )
    assert tokens.access_token("a" * 24) == "AT-A" and oauth.forms == []
    now[0] += 3600 - 60  # inside the early-refresh window
    assert tokens.access_token("a" * 24) == "AT-A2"
    assert store.grant("a" * 24).refresh_token == "RT-A2"  # type: ignore[union-attr]


def test_refused_refresh_forgets_the_user(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    oauth = FakeOAuth()
    oauth.refuse_refresh = True
    store = GrantStore(tmp_path / "s.json")
    tokens = UserTokens(store, TokenEndpoint(SETTINGS, httpx.MockTransport(oauth)))
    store.put_grant(Grant("a" * 24, "Pat", "AT-A", "RT-A", 0))
    sid = store.new_session("a" * 24)
    with pytest.raises(AuthError, match="refused"):
        tokens.access_token("a" * 24)
    assert store.grant("a" * 24) is None and store.session_user(sid) is None
    # The log says why (revoked vs wrong client), without the secret or the token.
    assert "refused refresh_token (400): invalid_grant" in caplog.text
    assert "client-secret" not in caplog.text and "RT-A" not in caplog.text


def test_bearer_retries_once_after_a_401() -> None:
    calls: list[bool] = []

    def get(force: bool) -> str:
        calls.append(force)
        return "AT-A2" if force else "stale"

    seen: list[str] = []
    with OnshapeClient(
        "https://cad.onshape.com",
        BearerAuth(get),
        transport=httpx.MockTransport(onshape_with_bearer(seen)),
    ) as c:
        assert c.whoami() == ("a" * 24, "Pat")
    assert seen == ["stale", "AT-A2"] and calls == [False, True]


# -- the browser flow ---------------------------------------------------------------


class SignedInServer:
    def __init__(self, cfg: Config, tmp: Path) -> None:
        self.oauth = FakeOAuth()
        self.seen: list[str] = []
        self.fake = FakeBambuddy(job_states=["completed"])
        cfg = dataclasses.replace(cfg, onshape_auth="oauth", oauth_client_id="CLIENTID123")
        assert cfg.oauth_redirect_uri == SETTINGS.redirect_uri
        signin = server.make_signin(
            cfg,
            "client-secret",
            tmp / "signins.json",
            onshape_as=lambda a: OnshapeClient(
                "https://cad.onshape.com",
                a,
                transport=httpx.MockTransport(onshape_with_bearer(self.seen)),
            ),
            transport=httpx.MockTransport(self.oauth),
        )

        def no_keys() -> OnshapeClient:
            raise AssertionError("the shared API keys must not be used in sign-in mode")

        self.svc = server.Service(
            cfg,
            onshape=no_keys,
            modules=fake_modules(cfg, self.fake),
            signin=signin,
        )
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(self.svc))
        threading.Thread(target=self.httpd.serve_forever, args=(0.01,), daemon=True).start()
        self.http = httpx.Client(base_url=f"http://127.0.0.1:{self.httpd.server_port}")

    def sign_in(self, code: str = "code-A") -> tuple[str, str]:
        """Run the pop-up flow; returns (top-level session id, panel session id)."""
        start = self.http.get("/auth/start", headers=NAV)
        state = parse_qs(urlsplit(start.headers["Location"]).query)["state"][0]
        nonce = cookies(start)["os2s_state"]
        back = self.http.get(
            f"/auth/callback?code={code}&state={state}",
            headers={**NAV, "Cookie": f"os2s_state={nonce}"},
        )
        assert back.status_code == 200, back.text
        claim = re.search(r'data-claim="([^"]+)"', back.text).group(1)  # type: ignore[union-attr]
        got = self.http.post("/auth/claim", data={"claim": claim}, headers=SAME)
        assert got.status_code == 200, got.text
        return cookies(back)["os2s"], cookies(got)["os2s_p"]


def cookies(r: httpx.Response) -> dict[str, str]:
    return {
        h.split("=", 1)[0]: h.split("=", 1)[1].split(";")[0]
        for h in r.headers.get_list("set-cookie")
    }


@pytest.fixture
def signed(cfg: Config, tmp_path: Path) -> Iterator[SignedInServer]:
    run = SignedInServer(cfg, tmp_path)
    yield run
    run.httpd.shutdown()


def test_panel_asks_to_sign_in_first(signed: SignedInServer) -> None:
    r = signed.http.get(PANEL, headers=FRAME)
    assert r.status_code == 200 and 'id="signin-button"' in r.text
    assert "/static/auth.js?v=" in r.text and signed.seen == []  # nothing read from Onshape


def test_revoked_sign_in_shows_the_sign_in_button(signed: SignedInServer) -> None:
    # E.g. after the Onshape app moved to another owner: the saved refresh token is refused.
    _, panel = signed.sign_in()
    store = signed.svc.signin.store  # type: ignore[union-attr]
    store.put_grant(dataclasses.replace(store.grant("a" * 24), expires_at=0))
    signed.oauth.refuse_refresh = True
    r = signed.http.get(PANEL, headers={**FRAME, "Cookie": f"os2s_p={panel}"})
    assert r.status_code == 200 and 'id="signin-button"' in r.text, r.text
    assert store.grant("a" * 24) is None  # forgotten, so a fresh sign-in starts clean


def test_sign_in_flow_sets_both_cookies_and_reads_as_the_user(signed: SignedInServer) -> None:
    start = signed.http.get("/auth/start", headers=NAV)
    assert start.status_code == 303
    loc = urlsplit(start.headers["Location"])
    q = parse_qs(loc.query)
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == "https://oauth.onshape.com/oauth/authorize"
    assert q["response_type"] == ["code"] and q["client_id"] == ["CLIENTID123"]
    assert q["redirect_uri"] == [SETTINGS.redirect_uri] and "client-secret" not in str(loc)
    state_cookie = start.headers["set-cookie"]
    assert "Path=/auth/" in state_cookie and "HttpOnly" in state_cookie

    top, panel = signed.sign_in()
    assert top == panel  # one session, delivered to the top-level and the panel cookie jar
    r = signed.http.get(PANEL, headers={**FRAME, "Cookie": f"os2s_p={panel}"})
    assert r.status_code == 200 and "Signed in as Pat" in r.text and 'id="panel"' in r.text
    assert "AT-A" in signed.seen  # parts listed with Pat's own token
    raw = (signed.svc.signin.store.path).read_text()  # type: ignore[union-attr]
    assert panel not in raw and json.loads(raw)["grants"]["a" * 24]["name"] == "Pat"


def test_panel_cookie_is_partitioned_third_party(signed: SignedInServer) -> None:
    start = signed.http.get("/auth/start", headers=NAV)
    state = parse_qs(urlsplit(start.headers["Location"]).query)["state"][0]
    back = signed.http.get(
        f"/auth/callback?code=code-A&state={state}",
        headers={**NAV, "Cookie": f"os2s_state={cookies(start)['os2s_state']}"},
    )
    top = next(h for h in back.headers.get_list("set-cookie") if h.startswith("os2s="))
    assert "SameSite=Lax" in top and "Secure" in top and "HttpOnly" in top
    claim = re.search(r'data-claim="([^"]+)"', back.text).group(1)  # type: ignore[union-attr]
    got = signed.http.post("/auth/claim", data={"claim": claim}, headers=SAME)
    panel = got.headers["set-cookie"]
    assert "SameSite=None" in panel and "Partitioned" in panel and "Secure" in panel


def test_sign_in_refusals(signed: SignedInServer) -> None:
    start = signed.http.get("/auth/start", headers=NAV)
    state = parse_qs(urlsplit(start.headers["Location"]).query)["state"][0]
    nonce = cookies(start)["os2s_state"]
    # Someone else's link: no matching state cookie in this browser.
    assert (
        signed.http.get(
            f"/auth/callback?code=code-A&state={state}",
            headers={**NAV, "Cookie": "os2s_state=other"},
        ).status_code
        == 403
    )
    # The state is single use, even with the right cookie.
    assert (
        signed.http.get(
            f"/auth/callback?code=code-A&state={state}",
            headers={**NAV, "Cookie": f"os2s_state={nonce}"},
        ).status_code
        == 403
    )
    # Not framed or fetched: the start page is a top-level navigation only.
    assert (
        signed.http.get("/auth/start", headers={**NAV, "Sec-Fetch-Dest": "iframe"}).status_code
        == 403
    )
    # Claims: unknown, cross-site.
    assert signed.http.post("/auth/claim", data={"claim": "nope"}, headers=SAME).status_code == 403
    cross = {**SAME, "Sec-Fetch-Site": "cross-site"}
    assert signed.http.post("/auth/claim", data={"claim": "x"}, headers=cross).status_code == 403
    # A cancelled sign-in.
    assert signed.http.get("/auth/callback?error=access_denied", headers=NAV).status_code == 401


def test_claim_is_single_use(signed: SignedInServer) -> None:
    start = signed.http.get("/auth/start", headers=NAV)
    state = parse_qs(urlsplit(start.headers["Location"]).query)["state"][0]
    back = signed.http.get(
        f"/auth/callback?code=code-A&state={state}",
        headers={**NAV, "Cookie": f"os2s_state={cookies(start)['os2s_state']}"},
    )
    claim = re.search(r'data-claim="([^"]+)"', back.text).group(1)  # type: ignore[union-attr]
    assert signed.http.post("/auth/claim", data={"claim": claim}, headers=SAME).status_code == 200
    assert signed.http.post("/auth/claim", data={"claim": claim}, headers=SAME).status_code == 403


def test_print_page_links_to_sign_in_and_comes_back(signed: SignedInServer) -> None:
    q = f"d={DOC}&wv=w&wvid={WS}&e={ELEM}&p=JHD"
    r = signed.http.get(f"/print?{q}", headers=NAV)
    assert r.status_code == 401 and "/auth/start?next=%2Fprint%3F" in r.text
    start = signed.http.get(f"/auth/start?next=/print%3F{q.replace('&', '%26')}", headers=NAV)
    state = parse_qs(urlsplit(start.headers["Location"]).query)["state"][0]
    back = signed.http.get(
        f"/auth/callback?code=code-A&state={state}",
        headers={**NAV, "Cookie": f"os2s_state={cookies(start)['os2s_state']}"},
    )
    assert f'data-next="/print?{q.replace("&", "&amp;")}"' in back.text
    page = signed.http.get(
        f"/print?{q}", headers={**NAV, "Cookie": f"os2s={cookies(back)['os2s']}"}
    )
    assert page.status_code == 200 and "Part 1" in page.text


def test_unsafe_next_is_dropped(signed: SignedInServer) -> None:
    start = signed.http.get("/auth/start?next=//evil.example/x", headers=NAV)
    state = parse_qs(urlsplit(start.headers["Location"]).query)["state"][0]
    back = signed.http.get(
        f"/auth/callback?code=code-A&state={state}",
        headers={**NAV, "Cookie": f"os2s_state={cookies(start)['os2s_state']}"},
    )
    assert 'data-next=""' in back.text


def test_previews_are_cached_per_user(signed: SignedInServer) -> None:
    _, pat = signed.sign_in("code-A")
    _, sam = signed.sign_in("code-B")
    fetch = {"Host": HOST, "Sec-Fetch-Site": "same-origin"}
    url = f"/panel/preview?d={DOC}&wv=w&wvid={WS}&e={ELEM}&p=JHD&orient=z-"
    assert signed.http.get(url, headers={**fetch, "Cookie": f"os2s_p={pat}"}).status_code == 200
    # Sam can't read the part: Pat's cached mesh must not leak to him.
    assert signed.http.get(url, headers={**fetch, "Cookie": f"os2s_p={sam}"}).status_code == 403


def test_model_link_remembers_who_made_it(signed: SignedInServer) -> None:
    _, pat = signed.sign_in()
    r = signed.http.get(PANEL, headers={**FRAME, "Cookie": f"os2s_p={pat}"})
    form = dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)">', r.text))
    link = signed.http.post(
        "/panel/model-link",
        data={**form, "p": "JHD", "orient": "z-"},
        headers={**SAME, "Cookie": f"os2s_p={pat}"},
    )
    assert link.status_code == 200, link.text
    path = link.json()["url"].split(HOST, 1)[1]
    signed.seen.clear()
    got = signed.http.get(path, headers={"Host": HOST})  # Bambu Studio: no cookies
    assert got.status_code == 200 and set(signed.seen) <= {"AT-A"} and signed.seen


def test_sign_out_ends_the_session_and_forgets_the_grant(signed: SignedInServer) -> None:
    _, pat = signed.sign_in()
    out = signed.http.post("/auth/sign-out", headers={**SAME, "Cookie": f"os2s_p={pat}"})
    assert out.status_code == 200 and "Max-Age=0" in out.headers["set-cookie"]
    r = signed.http.get(PANEL, headers={**FRAME, "Cookie": f"os2s_p={pat}"})
    assert 'id="signin-button"' in r.text
    assert signed.svc.signin.store.grant("a" * 24) is None  # type: ignore[union-attr]


def test_auth_routes_are_off_in_key_mode(cfg: Config) -> None:
    svc = server.Service(cfg, onshape=lambda: None, modules=Modules())  # type: ignore[arg-type,return-value]
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(svc))
    threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True).start()
    try:
        r = httpx.get(f"http://127.0.0.1:{httpd.server_port}/auth/start", headers=NAV)
        assert r.status_code == 500 and "enabled" in r.text
    finally:
        httpd.shutdown()
