from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from clipforge.api.deps import CurrentUser
from clipforge.core.urls import UnsafeURLError, detect_platform, parse_media_url, split_url_list
from clipforge.db.models import Project, Video
from clipforge.jobs.queue import enqueue
from clipforge.storage import get_storage

MAX_BATCH = 50


def _enqueue_stage(db: Session, video: Video, kind: str) -> None:
    stage = kind.split(".")[1]
    enqueue(
        db,
        kind,
        {"video_id": str(video.id)},
        dedupe_key=f"{stage}:{video.id}",
        workspace_id=video.workspace_id,
    )


def create_url_imports(
    db: Session,
    user: CurrentUser,
    project: Project,
    raw_urls: list[str],
    section: tuple[float, float] | None,
) -> list[Video]:
    urls: list[str] = []
    errors: list[str] = []
    for raw in raw_urls:
        try:
            urls.append(parse_media_url(raw))
        except UnsafeURLError as exc:
            errors.append(f"{raw[:80]}: {exc}")
    if errors:
        raise HTTPException(422, {"message": "Some links are invalid.", "errors": errors})
    if not urls:
        raise HTTPException(422, "Paste at least one video link.")
    if len(urls) > MAX_BATCH:
        raise HTTPException(422, f"At most {MAX_BATCH} links per import.")

    videos = []
    for url in dict.fromkeys(urls):
        video = Video(
            workspace_id=user.workspace_id,
            project_id=project.id,
            source_type="url",
            source_url=url,
            import_options={"section": list(section)} if section else {},
            title=url,
            platform=detect_platform(url),
            status="queued",
            created_by=user.id,
        )
        db.add(video)
        db.flush()
        _enqueue_stage(db, video, "video.ingest")
        videos.append(video)
    db.commit()
    return videos


def parse_import(urls: list[str], text: str | None) -> list[str]:
    return [*urls, *(split_url_list(text) if text else [])]


def retry(db: Session, video: Video) -> Video:
    """Resume a failed video from the earliest stage whose output is missing."""
    storage = get_storage()
    assets = video.assets or {}
    if not storage.exists(assets.get("source")) or not storage.exists(assets.get("timeline")):
        if video.source_type == "upload" and not storage.exists(assets.get("source")):
            raise HTTPException(409, "The uploaded file is gone; please upload it again.")
        kind, status = "video.ingest", "queued"
    elif video.has_audio and not storage.exists(assets.get("transcript")):
        kind, status = "video.transcribe", "transcribing"
    else:
        kind, status = "video.analyze", "analyzing"
    video.status, video.progress, video.error = status, 0.0, None
    _enqueue_stage(db, video, kind)
    db.commit()
    return video


def reanalyze(db: Session, video: Video) -> Video:
    if video.status not in ("ready", "failed"):
        raise HTTPException(409, "This video is still processing.")
    if not get_storage().exists((video.assets or {}).get("timeline")):
        raise HTTPException(409, "This video hasn't been processed yet.")
    video.status, video.progress, video.error = "analyzing", 0.0, None
    _enqueue_stage(db, video, "video.analyze")
    db.commit()
    return video


def import_summary(videos: list[Video]) -> list[dict[str, Any]]:
    return [{"id": str(v.id), "url": v.source_url} for v in videos]
