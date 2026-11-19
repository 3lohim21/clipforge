"""Worker process: claims jobs from the queues it serves and runs them on a thread pool.

Threads (not processes) are enough because the heavy lifting happens outside the GIL —
in ffmpeg/yt-dlp subprocesses and CTranslate2/OpenCV native code. Scale out by running
more worker containers; specialise them with WORKER_QUEUES (e.g. a GPU box that only
serves `transcribe`, CPU boxes that serve `render`).
"""

from __future__ import annotations

import logging
import os
import secrets
import signal
import socket
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import psycopg

from clipforge.config import Settings, get_settings
from clipforge.db import get_engine
from clipforge.jobs import queue as q
from clipforge.metrics import JOB_SECONDS, JOBS_TOTAL

log = logging.getLogger(__name__)


class JobContext:
    def __init__(self, job: q.Job, stop_event: threading.Event, settings: Settings) -> None:
        self.job = job
        self.settings = settings
        self._stop_event = stop_event

    @property
    def payload(self) -> dict[str, Any]:
        return self.job.payload

    def should_stop(self) -> bool:
        """Long loops may poll this to exit early during shutdown (the job is released)."""
        return self._stop_event.is_set()


@dataclass(frozen=True)
class Handler:
    run: Callable[[JobContext], None]
    # Called once a job has permanently failed (retries exhausted or PermanentJobError),
    # so the owning entity (video/render) can be marked failed with a readable message.
    on_failure: Callable[[q.Job, str], None] | None = None


def _plain_dsn(sqlalchemy_url: str) -> str:
    return sqlalchemy_url.replace("postgresql+psycopg://", "postgresql://", 1)


class Worker:
    def __init__(
        self,
        registry: dict[str, Handler],
        *,
        queues: list[str] | None = None,
        concurrency: int | None = None,
        settings: Settings | None = None,
        periodic: list[Callable[[], None]] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.registry = registry
        self.queues = queues or self.settings.worker_queues
        self.concurrency = max(1, concurrency or self.settings.worker_concurrency)
        self.id = f"{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(3)}"
        # Lightweight schedulers (e.g. "refresh stale view counts") run on the maintenance tick.
        self.periodic = periodic or []
        self.engine = get_engine()
        self._pool = ThreadPoolExecutor(self.concurrency, thread_name_prefix="job")
        self._active: dict[int, Future[None]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()

    # --- lifecycle --------------------------------------------------------------------------
    def stop(self, *_: object) -> None:
        if not self._stop.is_set():
            log.info("worker stopping", extra={"worker": self.id})
        self._stop.set()
        self._wake.set()

    def run(self, *, install_signals: bool = True, idle_exit_s: float | None = None) -> None:
        """Main loop. `idle_exit_s` (tests/batch mode) exits after being idle that long."""
        if install_signals:
            signal.signal(signal.SIGTERM, self.stop)
            signal.signal(signal.SIGINT, self.stop)
        log.info(
            "worker started",
            extra={"worker": self.id, "queues": ",".join(self.queues), "concurrency": self.concurrency},
        )
        threads = [
            threading.Thread(target=self._listen_loop, name="listen", daemon=True),
            threading.Thread(target=self._heartbeat_loop, name="heartbeat", daemon=True),
        ]
        for t in threads:
            t.start()

        last_maintenance = 0.0
        idle_since = time.monotonic()
        while not self._stop.is_set():
            if time.monotonic() - last_maintenance > 30:
                self._maintenance()
                last_maintenance = time.monotonic()

            claimed = self._fill_slots()
            with self._lock:
                busy = bool(self._active)
            if claimed or busy:
                idle_since = time.monotonic()
            elif idle_exit_s is not None and time.monotonic() - idle_since > idle_exit_s:
                break

            self._wake.wait(timeout=self.settings.job_poll_interval_s)
            self._wake.clear()

        self._shutdown()

    def run_until_idle(self, max_jobs: int = 1000) -> int:
        """Run every currently-runnable job inline, including jobs they enqueue, then
        return the number processed. Deterministic, so tests and one-off CLI runs use it."""
        processed = 0
        while processed < max_jobs:
            job = q.claim(self.engine, self.id, self.queues)
            if job is None:
                break
            self._execute(job)
            processed += 1
        return processed

    # --- claiming / execution ---------------------------------------------------------------
    def _fill_slots(self) -> int:
        claimed = 0
        while not self._stop.is_set():
            with self._lock:
                if len(self._active) >= self.concurrency:
                    break
            try:
                job = q.claim(self.engine, self.id, self.queues)
            except Exception:
                log.exception("claim failed")
                break
            if job is None:
                break
            claimed += 1
            future = self._pool.submit(self._execute, job)
            with self._lock:
                self._active[job.id] = future
            future.add_done_callback(lambda _f, jid=job.id: self._on_done(jid))
        return claimed

    def _on_done(self, job_id: int) -> None:
        with self._lock:
            self._active.pop(job_id, None)
        self._wake.set()  # a slot freed up: claim the next job immediately

    def _execute(self, job: q.Job) -> None:
        handler = self.registry.get(job.kind)
        started = time.monotonic()
        extra = {"job_id": job.id, "kind": job.kind, "attempt": job.attempts}
        if handler is None:
            q.fail(self.engine, job, self.id, f"no handler for {job.kind}", retryable=False)
            JOBS_TOTAL.labels(job.kind, "failed").inc()
            return
        log.info("job started", extra=extra)
        try:
            handler.run(JobContext(job, self._stop, self.settings))
        except q.PermanentJobError as exc:
            self._fail(job, handler, str(exc), retryable=False)
        except Exception as exc:
            if self._stop.is_set():
                # Interrupted by shutdown: hand it back untouched rather than count a failure.
                q.release(self.engine, [job.id], self.id)
                log.info("job released on shutdown", extra=extra)
                return
            log.exception("job error", extra=extra)
            self._fail(job, handler, f"{type(exc).__name__}: {exc}", retryable=True)
        else:
            q.complete(self.engine, job, self.id)
            JOBS_TOTAL.labels(job.kind, "succeeded").inc()
            log.info("job succeeded", extra={**extra, "seconds": round(time.monotonic() - started, 2)})
        finally:
            JOB_SECONDS.labels(job.kind).observe(time.monotonic() - started)

    def _fail(self, job: q.Job, handler: Handler, message: str, *, retryable: bool) -> None:
        outcome = q.fail(self.engine, job, self.id, message, retryable=retryable)
        JOBS_TOTAL.labels(job.kind, outcome).inc()
        log.warning("job %s", outcome, extra={"job_id": job.id, "kind": job.kind, "error": message[:300]})
        if outcome == "failed":
            self._run_failure_hook(handler, job, message)

    def _run_failure_hook(self, handler: Handler | None, job: q.Job, message: str) -> None:
        if handler and handler.on_failure:
            try:
                handler.on_failure(job, message)
            except Exception:
                log.exception("on_failure hook crashed", extra={"job_id": job.id})

    # --- background loops ---------------------------------------------------------------------
    def _listen_loop(self) -> None:
        dsn = _plain_dsn(self.settings.database_url)
        while not self._stop.is_set():
            try:
                with psycopg.connect(dsn, autocommit=True) as conn:
                    conn.execute(f"LISTEN {q.NOTIFY_CHANNEL}")
                    while not self._stop.is_set():
                        for note in conn.notifies(timeout=5.0):
                            if note.payload in self.queues or note.payload == "any":
                                self._wake.set()
            except Exception:
                log.warning("LISTEN connection lost; falling back to polling for 5s")
                self._stop.wait(5)

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.settings.job_heartbeat_s):
            with self._lock:
                ids = list(self._active)
            try:
                q.heartbeat(self.engine, ids, self.id)
            except Exception:
                log.exception("heartbeat failed")

    def _maintenance(self) -> None:
        try:
            for job in q.reap_stale(self.engine, self.settings.job_stale_after_s):
                log.warning("job failed after lost heartbeat", extra={"job_id": job.id, "kind": job.kind})
                self._run_failure_hook(self.registry.get(job.kind), job, "Processing was interrupted.")
            q.prune_finished(self.engine, self.settings.job_retention_days)
        except Exception:
            log.exception("maintenance failed")
        for task in self.periodic:
            try:
                task()
            except Exception:
                log.exception("periodic task failed", extra={"task": getattr(task, "__name__", "?")})

    def _shutdown(self) -> None:
        grace = 20.0
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            with self._lock:
                if not self._active:
                    break
            time.sleep(0.25)
        with self._lock:
            leftover = list(self._active)
        if leftover:
            # Hand unfinished work back so another worker restarts it right away.
            q.release(self.engine, leftover, self.id)
            log.info("released unfinished jobs", extra={"jobs": leftover})
        self._pool.shutdown(wait=False, cancel_futures=True)
        log.info("worker stopped", extra={"worker": self.id})
