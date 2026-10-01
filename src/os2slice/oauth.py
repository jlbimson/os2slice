"""Per-user Onshape sign-in (OAuth 2 authorization code flow), D-23.

Each coworker authorizes os2slice once; the service then reads Onshape with that
person's own access instead of one shared API key. Shapes per docs/ONSHAPE_API.md
("OAuth", to be verified live):

- authorize: GET  {oauth}/oauth/authorize?response_type=code&client_id&redirect_uri&state
- token:     POST {oauth}/oauth/token, form body, client credentials in the body;
             grant_type=authorization_code (code, redirect_uri) or refresh_token.
             Access tokens last ~60 min; each refresh returns a new refresh token.
- API calls: Authorization: Bearer <access token>; 401 → refresh once, retry.

Stored on the server only (0600 file, never logged): one grant per Onshape user and
the sign-in sessions pointing at it (session ids are kept hashed). Browsers hold only
a random session id in an HttpOnly cookie.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
from collections.abc import Callable, Generator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from os2slice import __version__
from os2slice.errors import AuthError, OnshapeError

log = logging.getLogger(__name__)

SESSION_DAYS = 30
STATE_TTL = 600.0  # seconds to finish signing in
CLAIM_TTL = 120.0  # seconds for the panel to pick up its session
REFRESH_EARLY = 120.0  # refresh this long before the access token expires
USER_ID_RE = re.compile(r"[0-9a-f]{24}")
SIGN_IN_AGAIN = "Sign in with Onshape again from the os2slice panel"


@dataclass
class Grant:
    """One Onshape user's tokens."""

    user_id: str
    name: str
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_at: float  # unix time


@dataclass(frozen=True)
class OAuthSettings:
    client_id: str
    client_secret: str = field(repr=False)
    redirect_uri: str  # https://<host>/auth/callback, registered on the OAuth app
    oauth_base_url: str = "https://oauth.onshape.com"

    def authorize_url(self, state: str) -> str:
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "state": state,
            }
        )
        return f"{self.oauth_base_url}/oauth/authorize?{query}"


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class TokenEndpoint:
    """The OAuth server's token endpoint (only the configured *.onshape.com host)."""

    def __init__(
        self, settings: OAuthSettings, transport: httpx.BaseTransport | None = None
    ) -> None:
        self.settings = settings
        self._transport = transport

    def exchange_code(self, code: str) -> dict[str, Any]:
        return self._post({"grant_type": "authorization_code", "code": code,
                           "redirect_uri": self.settings.redirect_uri})  # fmt: skip

    def refresh(self, refresh_token: str) -> dict[str, Any]:
        return self._post({"grant_type": "refresh_token", "refresh_token": refresh_token})

    def _post(self, form: dict[str, str]) -> dict[str, Any]:
        s = self.settings
        body = {**form, "client_id": s.client_id, "client_secret": s.client_secret}
        try:
            with httpx.Client(
                transport=self._transport, timeout=30.0, follow_redirects=False,
                headers={"User-Agent": f"os2slice/{__version__}", "Accept": "application/json"},
            ) as c:  # fmt: skip
                r = c.post(f"{s.oauth_base_url}/oauth/token", data=body)
        except httpx.TransportError as e:
            raise OnshapeError(f"Can't reach Onshape sign-in ({type(e).__name__})") from e
        if r.status_code in (400, 401):
            # invalid_grant: code used/expired, or the user revoked access
            raise AuthError("Onshape refused the sign-in", SIGN_IN_AGAIN)
        if not r.is_success:
            raise OnshapeError(f"Onshape sign-in failed ({r.status_code})", "Try again")
        try:
            data = r.json()
        except ValueError as e:
            raise OnshapeError("Onshape sign-in answered with something other than JSON") from e
        if not (
            isinstance(data, dict)
            and isinstance(data.get("access_token"), str)
            and isinstance(data.get("refresh_token"), str)
        ):
            raise OnshapeError("Onshape sign-in answered without tokens")
        return data


class GrantStore:
    """Grants and sessions in one JSON file (0600), written atomically."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._grants: dict[str, Grant] = {}
        self._sessions: dict[str, dict[str, Any]] = {}  # sha256(session id) -> user, expires
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            log.error("can't read %s, starting with no sign-ins: %s", self.path, type(e).__name__)
            return
        for uid, g in (data.get("grants") or {}).items():
            try:
                self._grants[uid] = Grant(**g)
            except TypeError:
                log.warning("dropping a malformed stored grant")
        self._sessions = {k: v for k, v in (data.get("sessions") or {}).items()
                          if isinstance(v, dict)}  # fmt: skip

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        data = {"grants": {u: asdict(g) for u, g in self._grants.items()},
                "sessions": self._sessions}  # fmt: skip
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, self.path)

    def put_grant(self, grant: Grant) -> None:
        with self._lock:
            self._grants[grant.user_id] = grant
            self._save()

    def grant(self, user_id: str) -> Grant | None:
        with self._lock:
            return self._grants.get(user_id)

    def drop_user(self, user_id: str) -> None:
        with self._lock:
            self._grants.pop(user_id, None)
            self._sessions = {k: v for k, v in self._sessions.items() if v["user"] != user_id}
            self._save()

    def new_session(self, user_id: str, now: float | None = None) -> str:
        now = time.time() if now is None else now
        sid = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions = {k: v for k, v in self._sessions.items() if v["expires"] > now}
            self._sessions[_hash(sid)] = {"user": user_id, "expires": now + SESSION_DAYS * 86400}
            self._save()
        return sid

    def session_user(self, sid: str, now: float | None = None) -> Grant | None:
        now = time.time() if now is None else now
        with self._lock:
            s = self._sessions.get(_hash(sid))
            if s is None or s["expires"] < now:
                return None
            return self._grants.get(s["user"])

    def end_session(self, sid: str) -> None:
        with self._lock:
            if self._sessions.pop(_hash(sid), None) is not None:
                self._save()


class SignIns:
    """Short-lived sign-in state: OAuth `state` values and panel claim codes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._states: dict[str, tuple[float, str, str]] = {}  # state -> expires, nonce hash, next
        self._claims: dict[str, tuple[float, str]] = {}  # code hash -> expires, session id

    def start(self, next_path: str) -> tuple[str, str]:
        """(state for the authorize URL, nonce for the browser's state cookie)."""
        state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        now = time.monotonic()
        with self._lock:
            self._states = {k: v for k, v in self._states.items() if v[0] > now}
            self._states[state] = (now + STATE_TTL, _hash(nonce), next_path)
        return state, nonce

    def finish(self, state: str, nonce: str) -> str | None:
        """The `next` path if state is known, fresh and from this browser; single use."""
        with self._lock:
            entry = self._states.pop(state, None)
        if entry is None or entry[0] < time.monotonic():
            return None
        if not secrets.compare_digest(entry[1], _hash(nonce)):
            return None
        return entry[2]

    def offer_claim(self, sid: str) -> str:
        code = secrets.token_urlsafe(24)
        now = time.monotonic()
        with self._lock:
            self._claims = {k: v for k, v in self._claims.items() if v[0] > now}
            self._claims[_hash(code)] = (now + CLAIM_TTL, sid)
        return code

    def redeem_claim(self, code: str) -> str | None:
        with self._lock:
            entry = self._claims.pop(_hash(code), None)
        if entry is None or entry[0] < time.monotonic():
            return None
        return entry[1]


class UserTokens:
    """Valid access tokens per user, refreshed on demand (one refresh at a time per user)."""

    def __init__(self, store: GrantStore, endpoint: TokenEndpoint,
                 clock: Callable[[], float] = time.time) -> None:  # fmt: skip
        self.store = store
        self.endpoint = endpoint
        self.clock = clock
        self._locks: dict[str, threading.Lock] = {}
        self._locks_lock = threading.Lock()

    def save_new(self, user_id: str, name: str, tokens: dict[str, Any]) -> Grant:
        grant = Grant(user_id, name, tokens["access_token"], tokens["refresh_token"],
                      self.clock() + float(tokens.get("expires_in") or 3600))  # fmt: skip
        self.store.put_grant(grant)
        return grant

    def access_token(self, user_id: str, force_refresh: bool = False) -> str:
        with self._locks_lock:
            lock = self._locks.setdefault(user_id, threading.Lock())
        with lock:
            grant = self.store.grant(user_id)
            if grant is None:
                raise AuthError("You're not signed in to os2slice", SIGN_IN_AGAIN)
            if not force_refresh and grant.expires_at - REFRESH_EARLY > self.clock():
                return grant.access_token
            try:
                tokens = self.endpoint.refresh(grant.refresh_token)
            except AuthError:
                log.warning("refresh refused for user %s; dropping the grant", user_id)
                self.store.drop_user(user_id)
                raise
            return self.save_new(user_id, grant.name, tokens).access_token


class BearerAuth(httpx.Auth):
    """Bearer token for one user; on a 401, refresh once and retry."""

    def __init__(self, tokens: UserTokens | Callable[[bool], str], user_id: str = "") -> None:
        if isinstance(tokens, UserTokens):
            self._get: Callable[[bool], str] = lambda force: tokens.access_token(user_id, force)
        else:
            self._get = tokens

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = f"Bearer {self._get(False)}"
        response = yield request
        if response.status_code == 401:
            request.headers["Authorization"] = f"Bearer {self._get(True)}"
            yield request


def static_bearer(token: str) -> BearerAuth:
    """For the one call right after sign-in (sessioninfo), before the grant is stored."""
    return BearerAuth(lambda _force: token)
