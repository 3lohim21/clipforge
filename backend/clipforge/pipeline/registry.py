"""Job kind -> handler wiring. Imported only by worker processes, so the API never loads
OpenCV / Whisper / yt-dlp into memory."""

from __future__ import annotations

import uuid
from collections.abc import Callable

from clipforge.jobs.queue import Job
from clipforge.jobs.worker import Handler
from clipforge.pipeline import analyze, ingest, maintenance, render, transcribe
from clipforge.pipeline.common import set_render, set_video


def _fail_video(job: Job, message: str) -> None:
    set_video(uuid.UUID(job.payload["video_id"]), status="failed", error=message[:500])


def _fail_render(job: Job, message: str) -> None:
    set_render(uuid.UUID(job.payload["render_id"]), status="failed", error=message[:500])


def build_registry() -> dict[str, Handler]:
    return {
        "video.ingest": Handler(ingest.run, _fail_video),
        "video.transcribe": Handler(transcribe.run, _fail_video),
        "video.analyze": Handler(analyze.run, _fail_video),
        "clip.render": Handler(render.run, _fail_render),
        "storage.cleanup": Handler(maintenance.cleanup),
        "posts.refresh": Handler(maintenance.refresh_posts),
    }


def periodic_tasks(queues: list[str]) -> list[Callable[[], None]]:
    return [maintenance.schedule_stats_refresh] if "maintenance" in queues else []
