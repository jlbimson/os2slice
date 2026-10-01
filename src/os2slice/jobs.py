"""Background print jobs and single-use CSRF tokens for the web service."""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

from os2slice.errors import Os2sliceError
from os2slice.logsetup import log_path

log = logging.getLogger(__name__)

JOB_ID_RE = re.compile(r"[A-Za-z0-9_-]{22}")
Work = Callable[[Callable[[str], None]], list[str]]


@dataclass
class Job:
    id: str
    user: str
    title: str
    created: float
    state: str = "queued"  # queued → running → done | failed
    steps: list[str] = field(default_factory=list)
    result: list[str] = field(default_factory=list)
    error: str = ""
    fix: str = ""
    back: str = ""  # where "Print another" goes (the panel)

    @property
    def finished(self) -> bool:
        return self.state in ("done", "failed")


class JobStore:
    """Keeps the last `limit` jobs in memory; runs them one at a time."""

    def __init__(self, limit: int = 100) -> None:
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._limit = limit

    def start(self, user: str, title: str, work: Work, back: str = "") -> Job:
        job = Job(secrets.token_urlsafe(16), user, title, time.time(), back=back)
        with self._lock:
            self._jobs[job.id] = job
            while len(self._jobs) > self._limit:
                self._jobs.popitem(last=False)
        threading.Thread(target=self._run, args=(job, work), daemon=True).start()
        return job

    def get(self, job_id: str, user: str) -> Job | None:
        if not JOB_ID_RE.fullmatch(job_id):
            return None
        with self._lock:
            job = self._jobs.get(job_id)
        return job if job is not None and job.user == user else None

    def recent(self, limit: int = 100) -> list[Job]:
        """Every user's jobs, newest first (for the config page, D-28)."""
        with self._lock:
            jobs = list(self._jobs.values())
        return jobs[::-1][:limit]

    def busy(self) -> bool:
        """True while a job is queued or running."""
        with self._lock:
            return any(not j.finished for j in self._jobs.values())

    def _run(self, job: Job, work: Work) -> None:
        with self._run_lock:
            job.state = "running"
            try:
                job.result = work(job.steps.append)
                job.state = "done"
            except Os2sliceError as e:
                log.error("job %s failed: %s", job.id, e.one_line(), exc_info=True)
                job.error, job.fix, job.state = e.message, e.fix, "failed"
            except Exception:
                log.exception("job %s crashed", job.id)
                job.error, job.fix = "Unexpected error", f"See {log_path()} on the server"
                job.state = "failed"


class CsrfTokens:
    """HMAC tokens bound to a user and the part shown; each can be used once."""

    def __init__(self, max_age: float = 1800, clock: Callable[[], float] = time.time) -> None:
        self._key = secrets.token_bytes(32)
        self._used: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()
        self._max_age = max_age
        self._clock = clock

    def issue(self, user: str, bound_to: str) -> str:
        stamp = str(int(self._clock()))
        nonce = secrets.token_urlsafe(12)
        return f"{stamp}.{nonce}.{self._mac(user, bound_to, stamp, nonce)}"

    def redeem(self, token: str, user: str, bound_to: str) -> bool:
        parts = token.split(".")
        if len(parts) != 3 or not parts[0].isdigit():
            return False
        stamp, nonce, mac = parts
        if not hmac.compare_digest(mac, self._mac(user, bound_to, stamp, nonce)):
            return False
        if not 0 <= self._clock() - int(stamp) <= self._max_age:
            return False
        with self._lock:
            if nonce in self._used:
                return False
            self._used[nonce] = None
            while len(self._used) > 10_000:
                self._used.popitem(last=False)
        return True

    def _mac(self, user: str, bound_to: str, stamp: str, nonce: str) -> str:
        msg = "\x1f".join((user, bound_to, stamp, nonce)).encode()
        return hmac.new(self._key, msg, hashlib.sha256).hexdigest()
