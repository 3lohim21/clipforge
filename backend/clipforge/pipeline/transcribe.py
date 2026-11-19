"""Stage 2 (only when no platform captions): local speech-to-text."""

from __future__ import annotations

import uuid

from clipforge.db import session_scope
from clipforge.db.models import Project, Video
from clipforge.jobs.queue import PermanentJobError, enqueue
from clipforge.jobs.worker import JobContext
from clipforge.pipeline.common import Progress, set_video
from clipforge.storage import get_storage, video_key
from clipforge.transcription import whisper
from clipforge.transcription.words import transcript_to_json


def run(ctx: JobContext) -> None:
    video_id = uuid.UUID(ctx.payload["video_id"])
    storage = get_storage()
    with session_scope() as s:
        video = s.get(Video, video_id)
        if video is None:
            return
        project = s.get(Project, video.project_id)
        language = (project.settings or {}).get("language") if project else None
        audio_key = video.assets.get("audio")
        duration_s = (video.duration_ms or 0) / 1000
    if not storage.exists(audio_key):
        raise PermanentJobError("Audio track is missing; re-import the video.")

    set_video(video_id, status="transcribing", progress=0.0)
    words, detected = whisper.transcribe_file(
        storage.path(audio_key),  # type: ignore[arg-type]
        settings=ctx.settings,
        duration_s=duration_s,
        language=language or None,
        on_progress=Progress(Video, video_id),
        should_stop=ctx.should_stop,
    )
    storage.write_json(
        video_key(video_id, "transcript.json"),
        transcript_to_json(words, source="whisper", language=detected),
    )
    with session_scope() as s:
        video = s.get(Video, video_id)
        if video is None:
            return
        video.assets = {**video.assets, "transcript": video_key(video_id, "transcript.json")}
        video.transcript_source, video.language = "whisper", detected
        video.status, video.progress = "analyzing", 0.0
        enqueue(
            s,
            "video.analyze",
            {"video_id": str(video_id)},
            dedupe_key=f"analyze:{video_id}",
            workspace_id=video.workspace_id,
        )
