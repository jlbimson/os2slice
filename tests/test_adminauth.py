from __future__ import annotations

import io
import json
import logging
import os
import stat
import threading
from pathlib import Path

import pytest

from os2slice import adminauth
from os2slice.adminauth import (
    MIN_LENGTH,
    SESSION_TTL,
    AdminStore,
    PasswordError,
    admin_path,
    set_password_interactive,
)

GOOD = "correct horse battery"


class Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


@pytest.fixture
def fast_scrypt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adminauth, "SCRYPT_N", 2**10)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path: Path, clock: Clock, fast_scrypt: None) -> AdminStore:
    return AdminStore(tmp_path / "state" / "admin.json", clock=clock)


def test_admin_path_follows_state_dir(tmp_path: Path) -> None:
    assert admin_path() == tmp_path / "state" / "os2slice" / "admin.json"


def test_real_parameters_hash_and_verify(tmp_path: Path) -> None:
    s = AdminStore(tmp_path / "admin.json")
    assert not s.has_password()
    assert not s.verify(GOOD)
    s.set_password(GOOD)
    assert s.has_password()
    assert s.verify(GOOD)
    assert not s.verify(GOOD + "x")
    data = json.loads((tmp_path / "admin.json").read_text())
    assert data["scrypt"]["n"] == 2**15
    assert data["scrypt"]["r"] == 8
    assert data["scrypt"]["p"] == 1
    assert len(bytes.fromhex(data["scrypt"]["salt"])) == 32
    assert GOOD not in (tmp_path / "admin.json").read_text()


def test_salt_is_random(store: AdminStore) -> None:
    store.set_password(GOOD)
    first = store.path.read_text()
    store.set_password(GOOD)
    assert store.path.read_text() != first
    assert store.verify(GOOD)


def test_file_mode_and_no_temp_left(store: AdminStore) -> None:
    store.set_password(GOOD)
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert os.listdir(store.path.parent) == ["admin.json"]


def test_persists_across_instances(store: AdminStore, clock: Clock) -> None:
    store.set_password(GOOD)
    again = AdminStore(store.path, clock=clock)
    assert again.has_password()
    assert again.verify(GOOD)


@pytest.mark.parametrize("pw", ["", "short", "x" * (MIN_LENGTH - 1), "y" * 1025])
def test_password_rules(store: AdminStore, pw: str) -> None:
    with pytest.raises(PasswordError) as e:
        store.set_password(pw)
    if pw:
        assert pw not in str(e.value)
    assert not store.has_password()


def test_min_length_exactly(store: AdminStore) -> None:
    store.set_password("a" * MIN_LENGTH)
    assert store.verify("a" * MIN_LENGTH)


def test_unicode_is_normalized(store: AdminStore) -> None:
    store.set_password("café au lait!!")  # precomposed
    assert store.verify("café au lait!!")  # decomposed


def test_verify_rejects_non_str_and_huge(store: AdminStore) -> None:
    store.set_password(GOOD)
    assert not store.verify(None)  # type: ignore[arg-type]
    assert not store.verify("z" * 5000)


def test_corrupt_file_means_no_password(store: AdminStore, clock: Clock) -> None:
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{not json")
    s = AdminStore(store.path, clock=clock)
    assert not s.has_password()
    assert not s.verify(GOOD)


def test_tampered_parameters_refused(store: AdminStore, clock: Clock) -> None:
    store.set_password(GOOD)
    data = json.loads(store.path.read_text())
    data["scrypt"]["n"] = 2**30
    store.path.write_text(json.dumps(data))
    assert not AdminStore(store.path, clock=clock).has_password()


# ---- rate limit ----


def test_backoff_per_client(store: AdminStore, clock: Clock) -> None:
    for _ in range(4):
        store.record_failure("10.0.0.5")
        assert store.attempt_allowed("10.0.0.5")
    store.record_failure("10.0.0.5")  # 5th
    assert not store.attempt_allowed("10.0.0.5")
    assert store.retry_after("10.0.0.5") == pytest.approx(30)
    assert store.attempt_allowed("10.0.0.6")  # others unaffected
    clock.advance(30)
    assert store.attempt_allowed("10.0.0.5")
    store.record_failure("10.0.0.5")  # 6th: 60 s
    assert store.retry_after("10.0.0.5") == pytest.approx(60)
    clock.advance(60)
    store.record_failure("10.0.0.5")  # 7th: 120 s
    assert store.retry_after("10.0.0.5") == pytest.approx(120)


def test_backoff_caps_at_15_minutes(store: AdminStore, clock: Clock) -> None:
    for _ in range(30):
        store.record_failure("c")
        clock.advance(store.retry_after("c"))
    store.record_failure("c")
    assert store.retry_after("c") == pytest.approx(15 * 60)


def test_success_clears_client(store: AdminStore) -> None:
    for _ in range(5):
        store.record_failure("c")
    assert not store.attempt_allowed("c")
    store.record_success("c")
    assert store.attempt_allowed("c")


def test_idle_record_forgotten(store: AdminStore, clock: Clock) -> None:
    for _ in range(4):
        store.record_failure("c")
    clock.advance(3601)
    store.record_failure("other")  # triggers pruning
    store.record_failure("c")
    assert store.attempt_allowed("c")  # counted from 1 again


def test_global_cap(store: AdminStore, clock: Clock) -> None:
    for i in range(20):
        store.record_failure(f"10.0.0.{i}")  # 1 failure each: no client lockout
    assert not store.attempt_allowed("10.0.0.200")
    assert store.retry_after("10.0.0.200") == pytest.approx(30)
    clock.advance(30)
    assert store.attempt_allowed("10.0.0.200")


def test_failures_dont_log_client_secrets(
    store: AdminStore, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    store.set_password(GOOD)
    store.verify("wrong password!!")
    store.record_failure("c")
    token = store.create_session()
    store.check_session(token)
    assert GOOD not in caplog.text
    assert token not in caplog.text


# ---- sessions ----


def test_session_lifecycle(store: AdminStore, clock: Clock) -> None:
    store.set_password(GOOD)
    t = store.create_session()
    assert len(t) >= 43  # 256 bits, urlsafe base64
    assert store.check_session(t)
    assert not store.check_session(t + "x")
    assert not store.check_session("")
    assert not store.check_session(None)  # type: ignore[arg-type]
    clock.advance(SESSION_TTL - 1)
    assert store.check_session(t)
    clock.advance(1)
    assert not store.check_session(t)


def test_session_stored_hashed_only(store: AdminStore) -> None:
    store.set_password(GOOD)
    t = store.create_session()
    assert t not in store._sessions
    assert t not in store.path.read_text()


def test_revoke_and_revoke_all(store: AdminStore) -> None:
    store.set_password(GOOD)
    a, b, c = (store.create_session() for _ in range(3))
    store.revoke(a)
    assert not store.check_session(a)
    assert store.check_session(b)
    store.revoke_all()
    assert not store.check_session(b)
    assert not store.check_session(c)


def test_expired_sessions_pruned(store: AdminStore, clock: Clock) -> None:
    store.set_password(GOOD)
    store.create_session()
    clock.advance(SESSION_TTL + 1)
    store.create_session()
    assert len(store._sessions) == 1


def test_session_cap(store: AdminStore, clock: Clock) -> None:
    store.set_password(GOOD)
    first = store.create_session()
    clock.advance(1)
    for _ in range(adminauth.MAX_SESSIONS):
        store.create_session()
    assert len(store._sessions) == adminauth.MAX_SESSIONS
    assert not store.check_session(first)


def test_sessions_not_persisted(store: AdminStore, clock: Clock) -> None:
    store.set_password(GOOD)
    t = store.create_session()
    assert not AdminStore(store.path, clock=clock).check_session(t)


def test_new_password_ends_sessions(store: AdminStore) -> None:
    store.set_password(GOOD)
    t = store.create_session()
    store.set_password("another long password")
    assert not store.check_session(t)


def test_password_set_by_other_process_ends_sessions(store: AdminStore, clock: Clock) -> None:
    store.set_password(GOOD)
    t = store.create_session()
    AdminStore(store.path, clock=clock).set_password("set from the CLI!")
    assert store.verify("set from the CLI!")
    assert not store.check_session(t)


def test_password_removed_ends_sessions(store: AdminStore) -> None:
    store.set_password(GOOD)
    t = store.create_session()
    store.path.unlink()
    assert not store.check_session(t)
    assert not store.has_password()


def test_concurrent_use(store: AdminStore) -> None:
    store.set_password(GOOD)
    errors: list[BaseException] = []

    def work(i: int) -> None:
        try:
            for _ in range(50):
                t = store.create_session()
                store.check_session(t)
                store.record_failure(f"c{i}")
                store.attempt_allowed(f"c{i}")
                store.revoke(t)
            if i % 4 == 0:
                store.set_password(GOOD)
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert not errors
    assert store.verify(GOOD)
    assert os.listdir(store.path.parent) == ["admin.json"]


# ---- interactive ----


def run_interactive(store: AdminStore, *answers: str) -> tuple[bool, str]:
    it = iter(answers)
    out = io.StringIO()

    def prompt(_msg: str) -> str:
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None

    ok = set_password_interactive(store, prompt=prompt, out=out)
    return ok, out.getvalue()


def test_interactive_sets(store: AdminStore) -> None:
    ok, out = run_interactive(store, GOOD, GOOD)
    assert ok
    assert store.verify(GOOD)
    assert GOOD not in out


def test_interactive_mismatch_then_ok(store: AdminStore) -> None:
    ok, out = run_interactive(store, GOOD, GOOD + "!", "short", GOOD, GOOD)
    assert ok
    assert "don't match" in out
    assert "at least 12" in out
    assert store.verify(GOOD)


def test_interactive_gives_up(store: AdminStore) -> None:
    ok, out = run_interactive(store, GOOD, "a" * 12, GOOD, "b" * 12, GOOD, "c" * 12)
    assert not ok
    assert "not changed" in out
    assert not store.has_password()


def test_interactive_eof(store: AdminStore) -> None:
    ok, _ = run_interactive(store, GOOD)
    assert not ok
    assert not store.has_password()
