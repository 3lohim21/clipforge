from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from clipforge.api.deps import CurrentUser, current_user, get_db
from clipforge.api.media import IMMUTABLE, media_response
from clipforge.api.schemas import ClipIn, ClipOut, ClipPatch, RenderIn, RenderOut
from clipforge.db.models import Clip, Project, Render, Video
from clipforge.services import clips as clip_service
from clipforge.services.access import scoped
from clipforge.services.projects import schedule_media_cleanup
from clipforge.storage import render_key

router = APIRouter(prefix="/api", tags=["clips"])


@router.get("/videos/{video_id}/clips", response_model=list[ClipOut])
def list_clips(
    video_id: uuid.UUID, user: CurrentUser = Depends(current_user), db: Session = Depends(get_db)
) -> list[ClipOut]:
    scoped(db, Video, video_id, user)
    clips = list(db.scalars(select(Clip).where(Clip.video_id == video_id).order_by(Clip.start_ms)))
    latest = clip_service.latest_renders(db, [c.id for c in clips])
    return [ClipOut.build(c, latest.get(c.id)) for c in clips]


@router.post("/videos/{video_id}/clips", response_model=ClipOut, status_code=201)
def create_clip(
    video_id: uuid.UUID,
    body: ClipIn,
    user: CurrentUser = Depends(current_user),
    db: Session = Depends(get_db),
) -> ClipOut:
    video = scoped(db, Video, video_id, user)
    if video.status != "ready":
        raise HTTPException(409, "Wait until the video finishes processing.")
    clip_service.validate_range(video, body.start_ms, body.end_ms)
    project = db.get(Project, video.project_id)
    edit = body.edit.model_dump() if body.edit else clip_service.default_edit(project)
    clip = Clip(
        workspace_id=video.workspace_id,
        project_id=video.project_id,
        video_id=video.id,
        origin="manual",
        status="saved",
        start_ms=body.start_ms,
        end_ms=min(body.end_ms, video.duration_ms or body.end_ms),
        title=body.title.strip() or "Untitled clip",
        edit=edit,
        created_by=user.id,
    )
    db.add(clip)
    db.commit()
    return ClipOut.build(clip)


@router.patch("/clips/{clip_id}", response_model=ClipOut)
def update_clip(
    clip_id: uuid.UUID,
    body: ClipPatch,
    user: CurrentUser = Depends(current_user),
    db: Session = Depends(get_db),
) -> ClipOut:
    clip = scoped(db, Clip, clip_id, user, lock=True)
    video = db.get(Video, clip.video_id)
    assert video is not None
    clip_service.apply_patch(clip, video, body)
    db.commit()
    return ClipOut.build(clip, clip_service.latest_renders(db, [clip.id]).get(clip.id))


@router.delete("/clips/{clip_id}", status_code=204)
def delete_clip(
    clip_id: uuid.UUID, user: CurrentUser = Depends(current_user), db: Session = Depends(get_db)
) -> None:
    clip = scoped(db, Clip, clip_id, user)
    render_ids = list(db.scalars(select(Render.id).where(Render.clip_id == clip.id)))
    prefixes = [k for rid in render_ids for k in (render_key(rid, "mp4"), render_key(rid, "jpg"))]
    if prefixes:
        schedule_media_cleanup(db, prefixes, user.workspace_id)
    db.delete(clip)
    db.commit()


@router.post("/clips/{clip_id}/render", response_model=RenderOut, status_code=202)
def render_clip(
    clip_id: uuid.UUID,
    body: RenderIn | None = None,
    user: CurrentUser = Depends(current_user),
    db: Session = Depends(get_db),
) -> RenderOut:
    clip = scoped(db, Clip, clip_id, user, lock=True)
    project = db.get(Project, clip.project_id)
    render = clip_service.request_render(db, user, clip, project, body.edit if body else None)
    return RenderOut.build(render, clip.title)


@router.get("/clips/{clip_id}/renders", response_model=list[RenderOut])
def list_clip_renders(
    clip_id: uuid.UUID, user: CurrentUser = Depends(current_user), db: Session = Depends(get_db)
) -> list[RenderOut]:
    clip = scoped(db, Clip, clip_id, user)
    renders = db.scalars(select(Render).where(Render.clip_id == clip.id).order_by(Render.created_at.desc()))
    return [RenderOut.build(r, clip.title) for r in renders]


@router.get("/projects/{project_id}/clips", response_model=list[ClipOut])
def project_clips(
    project_id: uuid.UUID,
    status: str = Query("saved", pattern="^(suggested|saved|discarded|all)$"),
    limit: int = Query(200, ge=1, le=500),
    before: datetime | None = None,
    user: CurrentUser = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[ClipOut]:
    """The project's clip library across all its videos."""
    scoped(db, Project, project_id, user)
    query = (
        select(Clip, Video.title).join(Video, Video.id == Clip.video_id).where(Clip.project_id == project_id)
    )
    if status != "all":
        query = query.where(Clip.status == status)
    if before is not None:
        query = query.where(Clip.created_at < before)
    rows = db.execute(query.order_by(Clip.created_at.desc()).limit(limit)).all()
    latest = clip_service.latest_renders(db, [c.id for c, _ in rows])
    return [ClipOut.build(c, latest.get(c.id), title) for c, title in rows]


# --- renders ------------------------------------------------------------------------------------


@router.get("/renders/{render_id}", response_model=RenderOut)
def get_render(
    render_id: uuid.UUID, user: CurrentUser = Depends(current_user), db: Session = Depends(get_db)
) -> RenderOut:
    render = scoped(db, Render, render_id, user)
    clip = db.get(Clip, render.clip_id)
    return RenderOut.build(render, clip.title if clip else None)


@router.get("/renders/{render_id}/file")
def render_file(
    render_id: uuid.UUID,
    download: bool = False,
    user: CurrentUser = Depends(current_user),
    db: Session = Depends(get_db),
):  # type: ignore[no-untyped-def]
    render = scoped(db, Render, render_id, user)
    if render.status != "done":
        raise HTTPException(409, "This export isn't finished yet.")
    name = None
    if download:
        clip = db.get(Clip, render.clip_id)
        name = f"{(clip.title if clip else 'clip')[:80]}.mp4"
    return media_response(
        render.output_key, media_type="video/mp4", cache_control=IMMUTABLE, download_name=name
    )


@router.get("/renders/{render_id}/thumbnail")
def render_thumbnail(
    render_id: uuid.UUID, user: CurrentUser = Depends(current_user), db: Session = Depends(get_db)
):  # type: ignore[no-untyped-def]
    render = scoped(db, Render, render_id, user)
    return media_response(render.thumbnail_key, media_type="image/jpeg", cache_control=IMMUTABLE)


@router.delete("/renders/{render_id}", status_code=204)
def delete_render(
    render_id: uuid.UUID, user: CurrentUser = Depends(current_user), db: Session = Depends(get_db)
) -> None:
    render = scoped(db, Render, render_id, user)
    schedule_media_cleanup(
        db, [render_key(render.id, "mp4"), render_key(render.id, "jpg")], user.workspace_id
    )
    db.delete(render)
    db.commit()


@router.get("/projects/{project_id}/renders", response_model=list[RenderOut])
def project_renders(
    project_id: uuid.UUID,
    limit: int = Query(100, ge=1, le=500),
    before: datetime | None = None,
    user: CurrentUser = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[RenderOut]:
    scoped(db, Project, project_id, user)
    query = (
        select(Render, Clip.title)
        .join(Clip, Clip.id == Render.clip_id)
        .where(Render.project_id == project_id)
    )
    if before is not None:
        query = query.where(Render.created_at < before)
    rows = db.execute(query.order_by(Render.created_at.desc()).limit(limit)).all()
    return [RenderOut.build(r, title) for r, title in rows]
