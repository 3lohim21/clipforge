"""Durable job queue on PostgreSQL.

Why Postgres instead of Redis/RabbitMQ: video jobs are few and long (seconds to minutes),
so throughput is never the bottleneck — correctness is. Enqueueing inside the same
transaction as the business write means "video row exists but its job was lost" cannot
happen, and there is one fewer stateful service to run, back up and monitor.

Mechanics:
* claim:     UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1)
* wake-up:   pg_notify on enqueue -> workers LISTEN, so pickup latency is ~ms, not a poll.
* liveness:  running jobs carry a heartbeat; jobs whose worker died are re-queued.
* retries:   exponential backoff with jitter; PermanentJobError skips retries.
* ownership: every state transition is guarded by locked_by, so a worker that lost its
             lease can never overwrite the job's new owner.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import Engine, func, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from clipforge.db.models import JobRow

NOTIFY_CHANNEL = "clipforge_jobs"

QUEUE_FOR_KIND: dict[str, str] = {
    "video.ingest": "ingest",
    "video.transcribe": "transcribe",
    "video.analyze": "analyze",
    "clip.render": "render",
    "storage.cleanup": "maintenance",
    "posts.refresh": "maintenance",
}


class PermanentJobError(Exception):
    """A failure that retrying cannot fix (bad link, private video, corrupt file)."""


@dataclass(frozen=True)
class Job:
    id: int
    queue: str
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    workspace_id: uuid.UUID | None


def enqueue(
    session: Session,
    kind: str,
    payload: dict[str, Any],
    *,
    priority: int = 0,
    delay_s: float = 0,
    max_attempts: int = 3,
    dedupe_key: str | None = None,
    workspace_id: uuid.UUID | None = None,
) -> int | None:
    """Insert a job in the caller's transaction. Returns None when a live job with the same
    dedupe_key already exists (idempotent enqueue)."""
    queue = QUEUE_FOR_KIND[kind]
    stmt = pg_insert(JobRow).values(
        queue=queue,
        kind=kind,
        payload=payload,
        priority=priority,
        run_at=func.now() + timedelta(seconds=delay_s),
        max_attempts=max_attempts,
        dedupe_key=dedupe_key,
        workspace_id=workspace_id,
    )
    if dedupe_key:
        stmt = stmt.on_conflict_do_nothing(
            index_elements=[JobRow.dedupe_key],
            index_where=JobRow.status.in_(("queued", "running")),
        )
    job_id = session.execute(stmt.returning(JobRow.id)).scalar_one_or_none()
    if job_id is not None:
        # Delivered at COMMIT, so workers never wake up for a job they can't see yet.
        session.execute(text("SELECT pg_notify(:ch, :q)"), {"ch": NOTIFY_CHANNEL, "q": queue})
    return job_id


_CLAIM = text(
    """
    UPDATE jobs
       SET status = 'running', attempts = attempts + 1, locked_by = :worker,
           locked_at = now(), heartbeat_at = now(), updated_at = now()
     WHERE id = (
           SELECT id FROM jobs
            WHERE status = 'queued' AND queue = ANY(:queues) AND run_at <= now()
            ORDER BY priority DESC, run_at, id
            LIMIT 1
              FOR UPDATE SKIP LOCKED)
    RETURNING id, queue, kind, payload, attempts, max_attempts, workspace_id
    """
)


def claim(engine: Engine, worker_id: str, queues: list[str]) -> Job | None:
    with engine.begin() as conn:
        row = conn.execute(_CLAIM, {"worker": worker_id, "queues": queues}).mappings().first()
    return Job(**row) if row else None


def complete(engine: Engine, job: Job, worker_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE jobs SET status = 'succeeded', finished_at = now(), updated_at = now(),"
                " locked_by = NULL, heartbeat_at = NULL"
                " WHERE id = :id AND locked_by = :worker AND status = 'running'"
            ),
            {"id": job.id, "worker": worker_id},
        )


def backoff_seconds(attempt: int) -> float:
    base = min(600.0, 15.0 * 2 ** max(0, attempt - 1))
    return base * random.uniform(0.8, 1.2)  # noqa: S311 - jitter, not crypto


def fail(engine: Engine, job: Job, worker_id: str, error: str, *, retryable: bool) -> str:
    """Record a failure. Returns 'retry' if the job was re-queued, else 'failed'."""
    error = error[-4000:]
    with engine.begin() as conn:
        if retryable and job.attempts < job.max_attempts:
            conn.execute(
                text(
                    "UPDATE jobs SET status = 'queued', run_at = now() + make_interval(secs => :delay),"
                    " last_error = :err, locked_by = NULL, locked_at = NULL, heartbeat_at = NULL,"
                    " updated_at = now() WHERE id = :id AND locked_by = :worker"
                ),
                {"delay": backoff_seconds(job.attempts), "err": error, "id": job.id, "worker": worker_id},
            )
            return "retry"
        conn.execute(
            text(
                "UPDATE jobs SET status = 'failed', last_error = :err, finished_at = now(),"
                " locked_by = NULL, heartbeat_at = NULL, updated_at = now()"
                " WHERE id = :id AND locked_by = :worker"
            ),
            {"err": error, "id": job.id, "worker": worker_id},
        )
        return "failed"


def heartbeat(engine: Engine, job_ids: list[int], worker_id: str) -> None:
    if not job_ids:
        return
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE jobs SET heartbeat_at = now()"
                " WHERE id = ANY(:ids) AND locked_by = :worker AND status = 'running'"
            ),
            {"ids": job_ids, "worker": worker_id},
        )


def release(engine: Engine, job_ids: list[int], worker_id: str) -> None:
    """Hand unfinished jobs back on graceful shutdown without burning an attempt."""
    if not job_ids:
        return
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE jobs SET status = 'queued', attempts = GREATEST(attempts - 1, 0), run_at = now(),"
                " locked_by = NULL, locked_at = NULL, heartbeat_at = NULL, updated_at = now()"
                " WHERE id = ANY(:ids) AND locked_by = :worker AND status = 'running'"
            ),
            {"ids": job_ids, "worker": worker_id},
        )
        conn.execute(text("SELECT pg_notify(:ch, 'any')"), {"ch": NOTIFY_CHANNEL})


def reap_stale(engine: Engine, stale_after_s: int) -> list[Job]:
    """Re-queue (or fail, if out of attempts) jobs whose worker stopped heartbeating.
    Returns the jobs that were failed so their on_failure hooks can run."""
    with engine.begin() as conn:
        rows = (
            conn.execute(
                text(
                    """
                UPDATE jobs
                   SET status = CASE WHEN attempts < max_attempts THEN 'queued' ELSE 'failed' END,
                       finished_at = CASE WHEN attempts < max_attempts THEN NULL ELSE now() END,
                       run_at = now(), locked_by = NULL, locked_at = NULL, heartbeat_at = NULL,
                       last_error = 'worker stopped responding (heartbeat lost)', updated_at = now()
                 WHERE status = 'running' AND heartbeat_at < now() - make_interval(secs => :stale)
                RETURNING id, queue, kind, payload, attempts, max_attempts, workspace_id, status
                """
                ),
                {"stale": stale_after_s},
            )
            .mappings()
            .all()
        )
    failed = []
    for row in rows:
        data = dict(row)
        if data.pop("status") == "failed":
            failed.append(Job(**data))
    return failed


def prune_finished(engine: Engine, retention_days: int) -> int:
    with engine.begin() as conn:
        result = conn.execute(
            text(
                "DELETE FROM jobs WHERE finished_at < now() - make_interval(days => :days)"
                " AND status IN ('succeeded', 'failed', 'canceled')"
            ),
            {"days": retention_days},
        )
    return result.rowcount or 0


def queue_depths(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT queue, status, count(*) AS n, "
                    " EXTRACT(EPOCH FROM now() - min(run_at)) AS oldest_s"
                    " FROM jobs WHERE status IN ('queued', 'running') GROUP BY queue, status"
                )
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]
