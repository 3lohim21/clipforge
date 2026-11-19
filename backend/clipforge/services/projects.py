from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from clipforge.api.schemas import ProjectOut, ProjectStats
from clipforge.core.project_settings import load_settings
from clipforge.db.models import Clip, Post, Project, Render, Video
from clipforge.jobs.queue import enqueue
from clipforge.storage import render_key, video_prefix


def project_stats(db: Session, ids: list[uuid.UUID]) -> dict[uuid.UUID, ProjectStats]:
    """All stats for N projects in 4 grouped queries (no N+1)."""
    stats = {pid: ProjectStats() for pid in ids}
    if not ids:
        return stats
    for pid, n in db.execute(
        select(Video.project_id, func.count()).where(Video.project_id.in_(ids)).group_by(Video.project_id)
    ):
        stats[pid].videos = n
    for pid, saved, suggested in db.execute(
        select(
            Clip.project_id,
            func.count().filter(Clip.status == "saved"),
            func.count().filter(Clip.status == "suggested"),
        )
        .where(Clip.project_id.in_(ids))
        .group_by(Clip.project_id)
    ):
        stats[pid].clips_saved, stats[pid].clips_suggested = saved, suggested
    for pid, n in db.execute(
        select(Render.project_id, func.count())
        .where(Render.project_id.in_(ids), Render.status == "done")
        .group_by(Render.project_id)
    ):
        stats[pid].renders = n
    for pid, n, views in db.execute(
        select(Post.project_id, func.count(), func.coalesce(func.sum(Post.views), 0))
        .where(Post.project_id.in_(ids))
        .group_by(Post.project_id)
    ):
        stats[pid].posts, stats[pid].views = n, int(views)
    return stats


def to_out(project: Project, stats: ProjectStats | None = None) -> ProjectOut:
    out = ProjectOut.model_validate(project)
    out.settings = load_settings(project.settings)
    if stats is not None:
        if out.settings.cpm_usd is not None:
            stats.est_revenue_usd = round(stats.views / 1000 * out.settings.cpm_usd, 2)
        out.stats = stats
    return out


def media_prefixes_for_videos(db: Session, video_ids: list[uuid.UUID]) -> list[str]:
    """Every storage path owned by these videos (their folders + all their renders)."""
    prefixes = [video_prefix(v) for v in video_ids]
    if video_ids:
        render_ids = db.scalars(
            select(Render.id).join(Clip, Clip.id == Render.clip_id).where(Clip.video_id.in_(video_ids))
        ).all()
        for rid in render_ids:
            prefixes += [render_key(rid, "mp4"), render_key(rid, "jpg")]
    return prefixes


def schedule_media_cleanup(db: Session, prefixes: list[str], workspace_id: uuid.UUID) -> None:
    # Delayed so an in-flight job touching these files finishes (or notices the row is gone) first.
    for i in range(0, len(prefixes), 500):
        enqueue(
            db, "storage.cleanup", {"prefixes": prefixes[i : i + 500]}, delay_s=120, workspace_id=workspace_id
        )
