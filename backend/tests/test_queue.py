"""Job queue semantics against real PostgreSQL."""

from __future__ import annotations

import threading

from sqlalchemy import text

from clipforge.db import get_engine, session_scope
from clipforge.jobs import queue as q
from clipforge.jobs.worker import Handler, Worker


def _status(job_id: int) -> tuple[str, int]:
    with get_engine().connect() as conn:
        row = conn.execute(text("SELECT status, attempts FROM jobs WHERE id = :id"), {"id": job_id}).one()
    return row.status, row.attempts


def test_enqueue_claim_complete(clean_db: None) -> None:
    with session_scope() as s:
        job_id = q.enqueue(s, "video.ingest", {"video_id": "x"})
    job = q.claim(get_engine(), "w1", ["ingest"])
    assert job and job.id == job_id and job.attempts == 1 and job.payload == {"video_id": "x"}
    assert q.claim(get_engine(), "w2", ["ingest"]) is None  # running jobs are invisible
    q.complete(get_engine(), job, "w1")
    assert _status(job_id) == ("succeeded", 1)


def test_dedupe_key_allows_one_live_job(clean_db: None) -> None:
    with session_scope() as s:
        first = q.enqueue(s, "video.analyze", {}, dedupe_key="analyze:1")
        second = q.enqueue(s, "video.analyze", {}, dedupe_key="analyze:1")
    assert first is not None and second is None
    job = q.claim(get_engine(), "w", ["analyze"])
    q.complete(get_engine(), job, "w")  # type: ignore[arg-type]
    with session_scope() as s:
        assert (
            q.enqueue(s, "video.analyze", {}, dedupe_key="analyze:1") is not None
        )  # finished: allowed again


def test_skip_locked_hands_each_job_to_exactly_one_worker(clean_db: None) -> None:
    with session_scope() as s:
        ids = {q.enqueue(s, "clip.render", {"n": i}) for i in range(40)}
    claimed: list[int] = []
    lock = threading.Lock()

    def grab(name: str) -> None:
        while (job := q.claim(get_engine(), name, ["render"])) is not None:
            with lock:
                claimed.append(job.id)

    threads = [threading.Thread(target=grab, args=(f"w{i}",)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(claimed) == sorted(ids)  # every job once, none twice


def test_priority_then_fifo(clean_db: None) -> None:
    with session_scope() as s:
        low = q.enqueue(s, "clip.render", {}, priority=0)
        high = q.enqueue(s, "clip.render", {}, priority=10)
    assert q.claim(get_engine(), "w", ["render"]).id == high  # type: ignore[union-attr]
    assert q.claim(get_engine(), "w", ["render"]).id == low  # type: ignore[union-attr]


def test_retry_backoff_then_permanent_failure(clean_db: None) -> None:
    with session_scope() as s:
        job_id = q.enqueue(s, "video.ingest", {}, max_attempts=2)
    job = q.claim(get_engine(), "w", ["ingest"])
    assert q.fail(get_engine(), job, "w", "boom", retryable=True) == "retry"  # type: ignore[arg-type]
    assert q.claim(get_engine(), "w", ["ingest"]) is None  # backoff delays the retry
    with get_engine().begin() as conn:
        conn.execute(text("UPDATE jobs SET run_at = now() WHERE id = :id"), {"id": job_id})
    job = q.claim(get_engine(), "w", ["ingest"])
    assert job.attempts == 2  # type: ignore[union-attr]
    assert q.fail(get_engine(), job, "w", "boom again", retryable=True) == "failed"  # type: ignore[arg-type]
    assert _status(job_id) == ("failed", 2)


def test_stale_lease_is_reaped_and_old_owner_cannot_complete(clean_db: None) -> None:
    with session_scope() as s:
        job_id = q.enqueue(s, "video.ingest", {}, max_attempts=3)
    job = q.claim(get_engine(), "dead-worker", ["ingest"])
    with get_engine().begin() as conn:
        conn.execute(
            text("UPDATE jobs SET heartbeat_at = now() - interval '10 minutes' WHERE id = :id"),
            {"id": job_id},
        )
    assert q.reap_stale(get_engine(), 60) == []  # re-queued, not failed
    new = q.claim(get_engine(), "new-worker", ["ingest"])
    assert new and new.id == job_id
    q.complete(get_engine(), job, "dead-worker")  # type: ignore[arg-type]  # stale owner: must be a no-op
    assert _status(job_id) == ("running", 2)
    q.complete(get_engine(), new, "new-worker")
    assert _status(job_id) == ("succeeded", 2)


def test_release_returns_job_without_burning_an_attempt(clean_db: None) -> None:
    with session_scope() as s:
        job_id = q.enqueue(s, "video.ingest", {})
    q.claim(get_engine(), "w", ["ingest"])
    q.release(get_engine(), [job_id], "w")
    assert _status(job_id) == ("queued", 0)


def test_worker_runs_handlers_and_calls_failure_hook(clean_db: None) -> None:
    seen: list[str] = []
    failures: list[str] = []

    def ok(ctx) -> None:  # type: ignore[no-untyped-def]
        seen.append(ctx.payload["n"])

    def bad(ctx) -> None:  # type: ignore[no-untyped-def]
        raise q.PermanentJobError("nope")

    registry = {
        "video.ingest": Handler(ok),
        "clip.render": Handler(bad, on_failure=lambda job, msg: failures.append(msg)),
    }
    with session_scope() as s:
        q.enqueue(s, "video.ingest", {"n": "a"})
        q.enqueue(s, "video.ingest", {"n": "b"})
        q.enqueue(s, "clip.render", {})
    processed = Worker(registry, queues=["ingest", "render"]).run_until_idle()
    assert processed == 3 and sorted(seen) == ["a", "b"] and failures == ["nope"]


def test_listen_notify_wakes_a_sleeping_worker_quickly(clean_db: None) -> None:
    done = threading.Event()
    registry = {"video.ingest": Handler(lambda ctx: done.set())}
    worker = Worker(registry, queues=["ingest"])
    worker.settings = worker.settings.model_copy(
        update={"job_poll_interval_s": 30.0}
    )  # polling alone would be slow
    thread = threading.Thread(target=worker.run, kwargs={"install_signals": False}, daemon=True)
    thread.start()
    threading.Event().wait(1.0)  # let it subscribe and go idle
    with session_scope() as s:
        q.enqueue(s, "video.ingest", {})
    assert done.wait(5.0), "NOTIFY should wake the worker well before the 30 s poll"
    worker.stop()
    thread.join(timeout=30)
