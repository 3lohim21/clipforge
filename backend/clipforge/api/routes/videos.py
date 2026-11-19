from __future__ import annotations

import os
import re
import uuid
from datetime import datetime
from pathlib import PurePath

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.requests import ClientDisconnect

from clipforge.api.deps import CurrentUser, current_user, get_db
from clipforge.api.media import IMMUTABLE, media_response
from clipforge.api.schemas import ImportIn, VideoOut
from clipforge.config import get_settings
from clipforge.core.ids import new_id
from clipforge.db import session_scope
from clipforge.db.models import Clip, Project, Video
from clipforge.jobs.queue import enqueue
from clipforge.rendering.captions import build_chunks
from clipforge.rendering.presets import get_preset
from clipforge.services import videos as video_service
from clipforge.services.access import scoped
from clipforge.services.projects import media_prefixes_for_videos, schedule_media_cleanup
from clipforge.storage import get_storage, video_key
from clipforge.transcription.words import transcript_from_json

router = APIRouter(prefix="/api", tags=["videos"])

UPLOAD_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".m4v",
    ".mkv",
    ".webm",
    ".avi",
    ".flv",
    ".ts",
    ".mts",
    ".mpg",
    ".mpeg",
    ".wmv",
    ".3gp",
}


def _clip_counts(db: Session, video_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not video_ids:
        return {}
    return dict(
        db.execute(
            select(Clip.video_id, func.count())
            .where(Clip.video_id.in_(video_ids), Clip.status != "discarded")
            .group_by(Clip.video_id)
        ).all()
    )  # type: ignore[arg-type]


@router.get("/projects/{project_id}/videos", response_model=list[VideoOut])
def list_videos(
    project_id: uuid.UUID,
    limit: int = Query(100, ge=1, le=500),
    before: datetime | None = None,
    user: CurrentUser = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[VideoOut]:
    scoped(db, Project, project_id, user)
    query = select(Video).where(Video.project_id == project_id)
    if before is not None:
        query = query.where(Video.created_at < before)
    videos = list(db.scalars(query.order_by(Video.created_at.desc()).limit(limit)))
    counts = _clip_counts(db, [v.id for v in videos])
    return [VideoOut.build(v, counts.get(v.id, 0)) for v in videos]


@router.post("/projects/{project_id}/videos", response_model=list[VideoOut], status_code=201)
def import_videos(
    project_id: uuid.UUID,
    body: ImportIn,
    user: CurrentUser = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[VideoOut]:
    project = scoped(db, Project, project_id, user)
    section = None
    if body.section_start_s is not None or body.section_end_s is not None:
        start, end = body.section_start_s or 0.0, body.section_end_s
        if end is None or end <= start:
            raise HTTPException(422, "The time range end must be after its start.")
        section = (start, end)
    videos = video_service.create_url_imports(
        db, user, project, video_service.parse_import(body.urls, body.text), section
    )
    return [VideoOut.build(v, 0) for v in videos]


@router.put("/projects/{project_id}/videos/upload", response_model=VideoOut, status_code=201)
async def upload_video(
    project_id: uuid.UUID,
    request: Request,
    filename: str = Query(..., min_length=1, max_length=255),
    user: CurrentUser = Depends(current_user),
) -> VideoOut:
    """Raw-body upload (`fetch(url, {method: 'PUT', body: file})`): streamed straight to the
    final location — no multipart parsing, no temp-file copy, constant memory."""
    settings = get_settings()

    def check_project() -> None:
        with session_scope() as s:
            scoped(s, Project, project_id, user)

    await run_in_threadpool(check_project)
    ext = PurePath(filename).suffix.lower()
    if ext not in UPLOAD_EXTENSIONS:
        raise HTTPException(415, f"Unsupported file type {ext or '(none)'}. Upload a video file.")
    declared = int(request.headers.get("content-length") or 0)
    if declared > settings.max_upload_bytes:
        raise HTTPException(413, "File is too large.")

    video_id = new_id()
    storage = get_storage()
    key = video_key(video_id, f"source{ext}")
    target = storage.ensure_parent(key)
    part = target.with_name(target.name + ".part")
    size = 0
    buffer = bytearray()
    try:
        with part.open("wb") as fh:
            async for chunk in request.stream():
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise HTTPException(413, "File is too large.")
                buffer += chunk
                if len(buffer) >= 8 * 1024 * 1024:  # batch disk writes off the event loop
                    await run_in_threadpool(fh.write, bytes(buffer))
                    buffer.clear()
            if buffer:
                await run_in_threadpool(fh.write, bytes(buffer))
        if size == 0:
            raise HTTPException(400, "The upload was empty.")
        os.replace(part, target)
    except (HTTPException, ClientDisconnect, OSError):
        part.unlink(missing_ok=True)
        storage.delete_prefix(f"videos/{video_id}")
        raise

    title = re.sub(r"[_\-]+", " ", PurePath(filename).stem).strip()[:200] or "Uploaded video"

    def create_row() -> VideoOut:
        with session_scope() as s:
            video = Video(
                id=video_id,
                workspace_id=user.workspace_id,
                project_id=project_id,
                source_type="upload",
                title=title,
                platform="upload",
                status="queued",
                size_bytes=size,
                assets={"source": key},
                created_by=user.id,
            )
            s.add(video)
            s.flush()
            enqueue(
                s,
                "video.ingest",
                {"video_id": str(video_id)},
                dedupe_key=f"ingest:{video_id}",
                workspace_id=user.workspace_id,
            )
            s.flush()
            s.refresh(video)
            return VideoOut.build(video, 0)

    return await run_in_threadpool(create_row)


@router.get("/videos/{video_id}", response_model=VideoOut)
def get_video(
    video_id: uuid.UUID, user: CurrentUser = Depends(current_user), db: Session = Depends(get_db)
) -> VideoOut:
    video = scoped(db, Video, video_id, user)
    return VideoOut.build(video, _clip_counts(db, [video.id]).get(video.id, 0))


@router.delete("/videos/{video_id}", status_code=204)
def delete_video(
    video_id: uuid.UUID, user: CurrentUser = Depends(current_user), db: Session = Depends(get_db)
) -> None:
    video = scoped(db, Video, video_id, user)
    schedule_media_cleanup(db, media_prefixes_for_videos(db, [video.id]), user.workspace_id)
    db.delete(video)
    db.commit()


@router.post("/videos/{video_id}/retry", response_model=VideoOut)
def retry_video(
    video_id: uuid.UUID, user: CurrentUser = Depends(current_user), db: Session = Depends(get_db)
) -> VideoOut:
    video = scoped(db, Video, video_id, user, lock=True)
    if video.status != "failed":
        raise HTTPException(409, "Only failed videos can be retried.")
    return VideoOut.build(video_service.retry(db, video))


@router.post("/videos/{video_id}/reanalyze", response_model=VideoOut)
def reanalyze_video(
    video_id: uuid.UUID, user: CurrentUser = Depends(current_user), db: Session = Depends(get_db)
) -> VideoOut:
    video = scoped(db, Video, video_id, user, lock=True)
    return VideoOut.build(video_service.reanalyze(db, video))


@router.get("/videos/{video_id}/transcript")
def get_transcript(
    video_id: uuid.UUID, user: CurrentUser = Depends(current_user), db: Session = Depends(get_db)
):  # type: ignore[no-untyped-def]
    video = scoped(db, Video, video_id, user)
    return media_response((video.assets or {}).get("transcript"), media_type="application/json")


@router.get("/videos/{video_id}/timeline")
def get_timeline(
    video_id: uuid.UUID, user: CurrentUser = Depends(current_user), db: Session = Depends(get_db)
):  # type: ignore[no-untyped-def]
    video = scoped(db, Video, video_id, user)
    return media_response((video.assets or {}).get("timeline"), media_type="application/json")


@router.get("/videos/{video_id}/captions")
def get_captions(
    video_id: uuid.UUID,
    preset: str = "bold",
    user: CurrentUser = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Caption chunks for the whole video, grouped exactly as the renderer will group them,
    so the editor preview matches the export word-for-word."""
    video = scoped(db, Video, video_id, user)
    key = (video.assets or {}).get("transcript")
    storage = get_storage()
    if not storage.exists(key):
        return {"preset": preset, "chunks": []}
    words = transcript_from_json(storage.read_json(key))  # type: ignore[arg-type]
    p = get_preset(preset)
    return {"preset": p.id, "chunks": [c.to_json() for c in build_chunks(words, p)]}


@router.get("/videos/{video_id}/media/{kind}")
def get_video_media(
    video_id: uuid.UUID, kind: str, user: CurrentUser = Depends(current_user), db: Session = Depends(get_db)
):  # type: ignore[no-untyped-def]
    video = scoped(db, Video, video_id, user)
    assets = video.assets or {}
    if kind == "playback":
        return media_response(
            assets.get("playback"), media_type="video/mp4", cache_control="private, max-age=86400"
        )
    if kind == "poster":
        return media_response(assets.get("poster"), media_type="image/jpeg", cache_control=IMMUTABLE)
    raise HTTPException(404, "Unknown media kind")
