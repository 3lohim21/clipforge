"""On-demand stage: export one clip to an MP4."""

from __future__ import annotations

import shutil
import uuid

from sqlalchemy import func

from clipforge.db import session_scope
from clipforge.db.models import Clip, Render, Video
from clipforge.jobs.queue import PermanentJobError
from clipforge.jobs.worker import JobContext
from clipforge.media.ffmpeg import probe
from clipforge.pipeline.common import Progress, set_render
from clipforge.rendering.compose import render_clip
from clipforge.storage import get_storage, render_key
from clipforge.transcription.words import transcript_from_json


def run(ctx: JobContext) -> None:
    render_id = uuid.UUID(ctx.payload["render_id"])
    settings, storage = ctx.settings, get_storage()
    with session_scope() as s:
        render = s.get(Render, render_id)
        if render is None:
            return
        if render.status == "done" and storage.exists(render.output_key):
            return  # already rendered (duplicate delivery)
        clip = s.get(Clip, render.clip_id)
        video = s.get(Video, clip.video_id) if clip else None
        if clip is None or video is None:
            return
        spec = dict(render.spec)
        assets = dict(video.assets)
    if not storage.exists(assets.get("source")):
        raise PermanentJobError("The source video is missing.")

    set_render(render_id, status="running", progress=0.0, error=None, started_at=func.now())
    src = storage.path(assets["source"])
    words = (
        transcript_from_json(storage.read_json(assets["transcript"]))
        if storage.exists(assets.get("transcript"))
        else []
    )
    out_key, thumb_key = render_key(render_id, "mp4"), render_key(render_id, "jpg")
    workdir = settings.tmp_dir / f"render-{render_id}"
    try:
        result = render_clip(
            spec,
            src=src,
            probe=probe(src),
            words=words,
            out_path=storage.ensure_parent(out_key),
            thumb_path=storage.ensure_parent(thumb_key),
            workdir=workdir,
            settings=settings,
            on_progress=Progress(Render, render_id, hi=0.99),
            should_stop=ctx.should_stop,
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    with session_scope() as s:
        render = s.get(Render, render_id)
        if render is None:
            storage.delete_prefix(out_key)
            storage.delete_prefix(thumb_key)
            return
        render.status, render.progress, render.finished_at = "done", 1.0, func.now()
        render.output_key, render.thumbnail_key = out_key, thumb_key
        render.size_bytes = storage.size(out_key)
        render.duration_ms, render.width, render.height = result.duration_ms, result.width, result.height
        # Cache a freshly computed face track on the clip so the next export/preview reuses it.
        clip = s.get(Clip, render.clip_id)
        if (
            clip
            and result.track
            and spec.get("reframe", "auto") == "auto"
            and not clip.edit.get("track")
            and clip.start_ms == spec["start_ms"]
            and clip.end_ms == spec["end_ms"]
        ):
            clip.edit = {**clip.edit, "track": result.track, "track_range": [clip.start_ms, clip.end_ms]}
