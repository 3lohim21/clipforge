"""Stage 3: find viral moments, optionally re-rank them with an LLM, pre-compute face tracks
for the editor preview, and publish them as suggested clips."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import delete, select

from clipforge.analysis.llm import rerank
from clipforge.analysis.scorer import MomentParams, auto_max_clips, find_moments
from clipforge.core.project_settings import load_settings
from clipforge.db import session_scope
from clipforge.db.models import Clip, Project, Video
from clipforge.jobs.worker import JobContext
from clipforge.media.audio import energy_from_u8_b64
from clipforge.pipeline.common import Progress, set_video
from clipforge.rendering.presets import get_layout
from clipforge.rendering.reframe import compute_track
from clipforge.storage import get_storage
from clipforge.transcription.words import transcript_from_json

log = logging.getLogger(__name__)

FACE_LAYOUTS = ("vertical_fill", "square_fill")


def _overlap(a0: int, a1: int, b0: int, b1: int) -> float:
    inter = min(a1, b1) - max(a0, b0)
    return inter / min(a1 - a0, b1 - b0) if inter > 0 else 0.0


def run(ctx: JobContext) -> None:
    video_id = uuid.UUID(ctx.payload["video_id"])
    settings, storage = ctx.settings, get_storage()
    with session_scope() as s:
        video = s.get(Video, video_id)
        if video is None:
            return
        project = s.get(Project, video.project_id)
        ps = load_settings(project.settings if project else None)
        assets, duration_ms = dict(video.assets), int(video.duration_ms or 0)
        width, height = video.width or 0, video.height or 0
        video_ctx = {
            "title": video.title,
            "uploader": video.uploader,
            "description": (video.meta or {}).get("description", ""),
        }
        kept_ranges = [
            (c.start_ms, c.end_ms)
            for c in s.scalars(select(Clip).where(Clip.video_id == video_id, Clip.status == "saved"))
        ]
    set_video(video_id, status="analyzing", progress=0.05)
    progress = Progress(Video, video_id, lo=0.05)

    words = (
        transcript_from_json(storage.read_json(assets["transcript"]))
        if storage.exists(assets.get("transcript"))
        else []
    )
    timeline: dict[str, Any] = (
        storage.read_json(assets["timeline"]) if storage.exists(assets.get("timeline")) else {}
    )
    energy = energy_from_u8_b64(timeline["energy_u8"]) if timeline.get("energy_u8") else None

    keep = ps.max_clips or auto_max_clips(duration_ms)
    use_llm = bool(ps.ai_rerank and settings.anthropic_api_key and words)
    params = MomentParams(
        min_len_ms=ps.min_clip_s * 1000,
        max_len_ms=ps.max_clip_s * 1000,
        target_len_ms=ps.target_clip_s * 1000,
        max_clips=min(settings.llm_max_candidates, keep * 2) if use_llm else keep + len(kept_ranges),
    )
    moments = find_moments(
        words, duration_ms=duration_ms, energy_db=energy, heatmap=timeline.get("heatmap"), params=params
    )
    # Don't re-suggest what the team already saved.
    moments = [m for m in moments if all(_overlap(m.start_ms, m.end_ms, a, b) < 0.5 for a, b in kept_ranges)]
    progress(0.3)
    llm_info: dict[str, Any] = {"used": False}
    if use_llm:
        moments, llm_info = rerank(moments, video=video_ctx, settings=settings, keep=keep)
    moments = moments[:keep]
    progress(0.45)

    # Pre-compute face tracks so the editor's 9:16 preview shows the real crop instantly and
    # renders skip detection. Cheap: only the clip ranges are decoded, at 4 fps.
    layout = get_layout(ps.layout)
    tracks: dict[int, list[tuple[float, float]]] = {}
    if (
        layout.id in FACE_LAYOUTS
        and width
        and height
        and width / height > layout.width / layout.height + 1e-3
    ):
        src = storage.path(assets["source"])
        for i, m in enumerate(moments):
            if ctx.should_stop():
                break
            tracks[i] = compute_track(
                src,
                m.start_ms / 1000,
                (m.end_ms - m.start_ms) / 1000,
                width,
                height,
                target_aspect=layout.width / layout.height,
                fps=settings.reframe_sample_fps,
            )
            progress(0.45 + 0.5 * (i + 1) / len(moments))

    base_edit = {
        "layout": layout.id,
        "captions": True,
        "caption_preset": ps.caption_preset,
        "caption_position": ps.caption_position,
        "highlight_color": ps.highlight_color,
        "title_overlay": ps.title_overlay,
        "reframe": "auto",
    }
    with session_scope() as s:
        video = s.get(Video, video_id)
        if video is None:
            return
        # Re-analysis replaces earlier suggestions but never touches saved/manual clips.
        s.execute(
            delete(Clip).where(Clip.video_id == video_id, Clip.origin == "ai", Clip.status == "suggested")
        )
        for i, m in enumerate(moments):
            edit = dict(base_edit)
            if i in tracks:
                edit |= {"track": tracks[i], "track_range": [m.start_ms, m.end_ms]}
            s.add(
                Clip(
                    workspace_id=video.workspace_id,
                    project_id=video.project_id,
                    video_id=video_id,
                    origin="ai",
                    status="suggested",
                    start_ms=m.start_ms,
                    end_ms=m.end_ms,
                    title=m.title or "Untitled moment",
                    hook=m.hook or None,
                    reason=m.reason,
                    hashtags=m.hashtags,
                    score=m.score,
                    score_breakdown=m.features,
                    edit=edit,
                )
            )
        video.status, video.progress, video.error = "ready", 1.0, None
        video.meta = {
            **video.meta,
            "analysis": {
                "clips": len(moments),
                "words": len(words),
                "llm": llm_info,
                "heatmap": bool(timeline.get("heatmap")),
            },
        }
    log.info(
        "analysis done", extra={"video_id": str(video_id), "clips": len(moments), "llm": llm_info.get("used")}
    )
