"""Admin password, login rate limit and sessions for the config page `/admin` (D-28).

The config page is the most sensitive page the service has, on a service open to the
LAN (D-17). This module holds the pieces that don't depend on HTTP:

- **Password.** Set only from the NUC side (`os2slice admin-password` or the add-on
  option), never from a browser, so a fresh install can't be claimed from the LAN. It
  is stored as an scrypt hash (n=2**15, r=8, p=1, 32-byte random salt, 32-byte key) in
  `admin.json` in the state dir (`admin_path()`), 0600, written atomically under a
  lock. Passwords are NFC-normalized, 12 to 1024 characters. `verify` compares in
  constant time. The service picks up a password set by another process (the CLI)
  on its next `verify`/`check_session`, and a changed password ends every session.
- **Rate limit** (in memory, per client id, e.g. the peer address): 5 failures lock
  that client out for 30 s, each further failure doubles it, up to 15 min. A global
  bucket (20 failures from anyone → the same backoff for everyone) caps a spray from
  many addresses; the cost is that such a spray can lock the admin out for up to
  15 min too, which is the safer failure. A success clears that client's record;
  idle records are forgotten after an hour.
- **Sessions:** a random 256-bit token (urlsafe base64) for the cookie, kept only as
  its SHA-256, absolute 12 h expiry, at most 50 at once (oldest dropped). They are
  **in memory only**: a restart signs the admin out, which is simpler and safer than
  another file of live credentials, and the admin page is rarely used.

The HTTP side (cookie `Secure; HttpOnly; SameSite=Strict`, CSRF, same-origin checks)
belongs to the server. Nothing here logs a password, hash or token.
"""

from __future__ import annotations

import getpass as _getpass
import hashlib
import hmac
import json
import logging
import secrets
import sys
import threading
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from os2slice.logsetup import state_dir
from os2slice.tomlwrite import write_atomic

log = logging.getLogger(__name__)

MIN_LENGTH = 12
MAX_LENGTH = 1024

SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 32
KEY_BYTES = 32
_SCRYPT_MAXMEM = 128 * 1024 * 1024  # n=2**15, r=8 needs 32 MiB, over OpenSSL's default cap

SESSION_TTL = 12 * 3600
MAX_SESSIONS = 50

FREE_FAILURES = 5  # failures before the first lockout (per client)
GLOBAL_FREE_FAILURES = 20  # failures from all clients together before a global lockout
FIRST_LOCKOUT = 30.0
MAX_LOCKOUT = 15 * 60.0
FORGET_AFTER = 3600.0  # drop an idle client record this long after its last failure
MAX_CLIENTS = 10_000

_GLOBAL = "\0global"  # can't collide with a real client id passed in by the server


def admin_path() -> Path:
    """Where the admin password hash lives: `<state dir>/admin.json`."""
    return state_dir() / "admin.json"


class PasswordError(ValueError):
    """The new password doesn't meet the rules. The message never contains it."""


def _normalize(password: str) -> str:
    return unicodedata.normalize("NFC", password)


def _check_rules(password: object) -> str:
    if not isinstance(password, str):
        raise PasswordError("the password must be text")
    pw = _normalize(password)
    if len(pw) < MIN_LENGTH:
        raise PasswordError(f"the password must be at least {MIN_LENGTH} characters")
    if len(pw) > MAX_LENGTH:
        raise PasswordError(f"the password must be at most {MAX_LENGTH} characters")
    return pw


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int, dklen: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=dklen, maxmem=_SCRYPT_MAXMEM
    )


def _sha256(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class _Backoff:
    failures: int = 0
    last_failure: float = 0.0
    locked_until: float = 0.0


@dataclass(frozen=True)
class _Hash:
    salt: bytes
    key: bytes
    n: int
    r: int
    p: int

    @classmethod
    def from_json(cls, d: Any) -> _Hash:
        s = d["scrypt"]
        n, r, p = int(s["n"]), int(s["r"]), int(s["p"])
        salt, key = bytes.fromhex(s["salt"]), bytes.fromhex(s["hash"])
        # A tampered file must not make verify() cost minutes or gigabytes.
        if not (2**10 <= n <= 2**20 and n & (n - 1) == 0 and 1 <= r <= 16 and 1 <= p <= 4):
            raise ValueError("scrypt parameters out of range")
        if len(salt) < 16 or not 16 <= len(key) <= 64:
            raise ValueError("bad salt or hash length")
        return cls(salt, key, n, r, p)

    def to_json(self, set_at: float) -> dict[str, Any]:
        return {
            "version": 1,
            "set_at": int(set_at),
            "scrypt": {
                "n": self.n,
                "r": self.r,
                "p": self.p,
                "salt": self.salt.hex(),
                "hash": self.key.hex(),
            },
        }


class AdminStore:
    """The admin password (on disk), login backoff and admin sessions (in memory).

    Thread-safe: one lock guards all state and every write of `admin.json`.
    """

    def __init__(self, path: Path, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self._clock = clock
        self._lock = threading.RLock()
        self._hash: _Hash | None = None
        self._file_sig: tuple[int, int, int] | None = None  # (mtime_ns, size, inode)
        self._sessions: dict[str, float] = {}  # sha256(token) -> expires
        self._backoff: dict[str, _Backoff] = {}
        with self._lock:
            self._reload()

    # ---- password ----

    def _sig(self) -> tuple[int, int, int] | None:
        try:
            st = self.path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size, st.st_ino)

    def _reload(self) -> None:
        """Re-read admin.json if it changed on disk (another process set the password)."""
        sig = self._sig()
        if sig == self._file_sig:
            return
        old = self._hash
        self._file_sig = sig
        if sig is None:
            self._hash = None
        else:
            try:
                self._hash = _Hash.from_json(json.loads(self.path.read_text(encoding="utf-8")))
            except (OSError, ValueError, KeyError, TypeError) as e:
                log.error("can't read %s, no admin password is set: %s", self.path,
                          type(e).__name__)  # fmt: skip
                self._hash = None
        if self._hash != old and old is not None:
            log.info("admin password changed on disk; ending admin sessions")
            self._sessions.clear()

    def has_password(self) -> bool:
        with self._lock:
            self._reload()
            return self._hash is not None

    def set_password(self, password: str) -> None:
        """Hash and store a new password; ends every admin session.

        Raises `PasswordError` when it breaks the rules (length 12 to 1024).
        """
        pw = _check_rules(password)
        salt = secrets.token_bytes(SALT_BYTES)
        key = _scrypt(pw, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P, KEY_BYTES)
        h = _Hash(salt, key, SCRYPT_N, SCRYPT_R, SCRYPT_P)
        with self._lock:
            write_atomic(self.path, json.dumps(h.to_json(self._clock())) + "\n", mode=0o600)
            self._hash = h
            self._file_sig = self._sig()
            self._sessions.clear()
        log.info("admin password set")

    def verify(self, password: str) -> bool:
        """True if `password` is the admin password (constant-time compare)."""
        with self._lock:
            self._reload()
            h = self._hash
        if h is None or not isinstance(password, str):
            return False
        pw = _normalize(password)
        if len(pw) > MAX_LENGTH:
            return False
        key = _scrypt(pw, h.salt, h.n, h.r, h.p, len(h.key))
        return hmac.compare_digest(key, h.key)

    # ---- login rate limit ----

    def _prune_backoff(self, now: float) -> None:
        self._backoff = {
            k: b
            for k, b in self._backoff.items()
            if b.locked_until > now or now - b.last_failure < FORGET_AFTER
        }
        if len(self._backoff) > MAX_CLIENTS:  # a flood of ids: keep the most recent
            keep = sorted(self._backoff.items(), key=lambda kv: kv[1].last_failure)
            self._backoff = dict(keep[-MAX_CLIENTS:])

    def retry_after(self, client_id: str) -> float:
        """Seconds until `client_id` may try again (0 when it may now)."""
        now = self._clock()
        with self._lock:
            until = max(
                (b.locked_until for k in (client_id, _GLOBAL) if (b := self._backoff.get(k))),
                default=0.0,
            )
        return max(0.0, until - now)

    def attempt_allowed(self, client_id: str) -> bool:
        """False while this client, or everyone, is locked out after failed logins."""
        return self.retry_after(client_id) <= 0

    def record_failure(self, client_id: str) -> None:
        now = self._clock()
        with self._lock:
            self._prune_backoff(now)
            locked = False
            for key, free in ((client_id, FREE_FAILURES), (_GLOBAL, GLOBAL_FREE_FAILURES)):
                b = self._backoff.setdefault(key, _Backoff())
                b.failures += 1
                b.last_failure = now
                if b.failures >= free:
                    lock = min(FIRST_LOCKOUT * 2 ** min(b.failures - free, 20), MAX_LOCKOUT)
                    b.locked_until = max(b.locked_until, now + lock)
                    locked = True
        log.warning("admin login failed%s", " (rate limit engaged)" if locked else "")

    def record_success(self, client_id: str) -> None:
        with self._lock:
            self._backoff.pop(client_id, None)

    # ---- sessions ----

    def _prune_sessions(self, now: float) -> None:
        self._sessions = {k: exp for k, exp in self._sessions.items() if exp > now}

    def create_session(self) -> str:
        """A new admin session token for the cookie. Only its hash is kept."""
        token = secrets.token_urlsafe(32)
        now = self._clock()
        with self._lock:
            self._prune_sessions(now)
            while len(self._sessions) >= MAX_SESSIONS:
                oldest = min(self._sessions, key=self._sessions.__getitem__)
                del self._sessions[oldest]
            self._sessions[_sha256(token)] = now + SESSION_TTL
        return token

    def check_session(self, token: str) -> bool:
        """True if `token` is a live session and an admin password is still set."""
        if not isinstance(token, str) or not token or len(token) > 256:
            return False
        now = self._clock()
        with self._lock:
            self._reload()
            if self._hash is None:
                self._sessions.clear()
                return False
            self._prune_sessions(now)
            return _sha256(token) in self._sessions

    def revoke(self, token: str) -> None:
        if not isinstance(token, str):
            return
        with self._lock:
            self._sessions.pop(_sha256(token), None)

    def revoke_all(self) -> None:
        with self._lock:
            self._sessions.clear()


def set_password_interactive(
    store: AdminStore,
    *,
    prompt: Callable[[str], str] = _getpass.getpass,
    out: TextIO | None = None,
    attempts: int = 3,
) -> bool:
    """Ask for the new admin password twice (no echo) and store it.

    True when set; False after `attempts` tries that were too short or didn't match,
    or on Ctrl-C/EOF. For the `os2slice admin-password` subcommand.
    """
    out = out or sys.stderr
    for _ in range(attempts):
        try:
            first = prompt(f"New admin password (at least {MIN_LENGTH} characters): ")
            try:
                _check_rules(first)
            except PasswordError as e:
                print(f"os2slice: {e}.", file=out)
                continue
            second = prompt("Repeat it: ")
        except (KeyboardInterrupt, EOFError):
            print("", file=out)
            return False
        if not hmac.compare_digest(_normalize(first).encode(), _normalize(second).encode()):
            print("os2slice: the passwords don't match.", file=out)
            continue
        store.set_password(first)
        print(f"Admin password saved in {store.path}. Admin sessions were signed out.", file=out)
        return True
    print("os2slice: admin password not changed.", file=out)
    return False


__all__ = [
    "MIN_LENGTH",
    "SESSION_TTL",
    "AdminStore",
    "PasswordError",
    "admin_path",
    "set_password_interactive",
]
