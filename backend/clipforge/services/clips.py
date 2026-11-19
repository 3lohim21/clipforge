from __future__ import annotations

import uuid
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from clipforge.api.deps import CurrentUser
from clipforge.api.schemas import ClipEdit, ClipPatch
from clipforge.core.project_settings import load_settings
from clipforge.db.models import Clip, Project, Render, Video
from clipforge.jobs.queue import enqueue

MIN_CLIP_MS = 1_000
MAX_CLIP_MS = 10 * 60 * 1000
_EDIT_KEYS = tuple(ClipEdit.model_fields)


def validate_range(video: Video, start_ms: int, end_ms: int) -> None:
    duration = video.duration_ms or 0
    if end_ms <= start_ms:
        raise HTTPException(422, "Clip end must be after its start.")
    if end_ms - start_ms < MIN_CLIP_MS:
        raise HTTPException(422, "Clips must be at least 1 second long.")
    if end_ms - start_ms > MAX_CLIP_MS:
        raise HTTPException(422, "Clips can be at most 10 minutes long.")
    if duration and end_ms > duration + 50:
        raise HTTPException(422, "Clip ends after the video does.")


def default_edit(project: Project | None) -> dict[str, Any]:
    ps = load_settings(project.settings if project else None)
    return ClipEdit(
        layout=ps.layout,
        caption_preset=ps.caption_preset,
        caption_position=ps.caption_position,
        highlight_color=ps.highlight_color,
        title_overlay=ps.title_overlay,
    ).model_dump()


def apply_patch(clip: Clip, video: Video, patch: ClipPatch) -> None:
    start = patch.start_ms if patch.start_ms is not None else clip.start_ms
    end = patch.end_ms if patch.end_ms is not None else clip.end_ms
    if (start, end) != (clip.start_ms, clip.end_ms):
        validate_range(video, start, end)
        clip.start_ms, clip.end_ms = start, min(end, video.duration_ms or end)
    if patch.title is not None:
        clip.title = patch.title.strip()
    if patch.status is not None:
        clip.status = patch.status
    if patch.hashtags is not None:
        clip.hashtags = [h.strip().lstrip("#")[:40] for h in patch.hashtags if h.strip()]
    edit = dict(clip.edit or {})
    if patch.edit is not None:
        edit.update(patch.edit.model_dump())
    # A face track is only valid for the exact range it was computed on.
    if edit.get("track_range") and edit["track_range"] != [clip.start_ms, clip.end_ms]:
        edit.pop("track", None)
        edit.pop("track_range", None)
    clip.edit = edit


def build_spec(clip: Clip, project: Project | None, overrides: ClipEdit | None) -> dict[str, Any]:
    ps = load_settings(project.settings if project else None)
    edit = {k: v for k, v in (clip.edit or {}).items() if k in _EDIT_KEYS}
    merged = ClipEdit(**{**default_edit(project), **edit, **(overrides.model_dump() if overrides else {})})
    spec: dict[str, Any] = merged.model_dump() | {
        "start_ms": clip.start_ms,
        "end_ms": clip.end_ms,
        "title": clip.title,
        "watermark": ps.watermark,
    }
    stored = clip.edit or {}
    if stored.get("track") and stored.get("track_range") == [clip.start_ms, clip.end_ms]:
        spec["track"] = stored["track"]
    return spec


def request_render(
    db: Session, user: CurrentUser, clip: Clip, project: Project | None, overrides: ClipEdit | None
) -> Render:
    spec = build_spec(clip, project, overrides)
    # Double-click protection: an identical export already in flight is returned as-is.
    for existing in db.scalars(
        select(Render).where(Render.clip_id == clip.id, Render.status.in_(("queued", "running")))
    ):
        if existing.spec == spec:
            return existing
    render = Render(
        workspace_id=clip.workspace_id,
        project_id=clip.project_id,
        clip_id=clip.id,
        status="queued",
        spec=spec,
        created_by=user.id,
    )
    db.add(render)
    if clip.status == "suggested":
        clip.status = "saved"  # exporting a suggestion accepts it
    db.flush()
    enqueue(
        db,
        "clip.render",
        {"render_id": str(render.id)},
        dedupe_key=f"render:{render.id}",
        priority=10,
        workspace_id=clip.workspace_id,
    )
    db.commit()
    return render


def latest_renders(db: Session, clip_ids: list[uuid.UUID]) -> dict[uuid.UUID, Render]:
    if not clip_ids:
        return {}
    rows = db.scalars(
        select(Render)
        .where(Render.clip_id.in_(clip_ids))
        .distinct(Render.clip_id)
        .order_by(Render.clip_id, Render.created_at.desc())
    ).all()
    return {r.clip_id: r for r in rows}
