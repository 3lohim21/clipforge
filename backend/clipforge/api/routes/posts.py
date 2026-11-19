"""Where clips got published, and how they're doing. Campaigns pay per 1,000 views, so the
project's CPM setting turns tracked views into an estimated revenue number."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from clipforge.api.deps import CurrentUser, current_user, get_db
from clipforge.api.schemas import PostIn, PostOut, PostsSummary
from clipforge.core.project_settings import load_settings
from clipforge.core.urls import UnsafeURLError, detect_platform, parse_media_url
from clipforge.db.models import Clip, Post, Project
from clipforge.jobs.queue import enqueue
from clipforge.services.access import scoped

router = APIRouter(prefix="/api", tags=["posts"])


@router.get("/projects/{project_id}/posts", response_model=PostsSummary)
def list_posts(
    project_id: uuid.UUID, user: CurrentUser = Depends(current_user), db: Session = Depends(get_db)
) -> PostsSummary:
    project = scoped(db, Project, project_id, user)
    rows = db.execute(
        select(Post, Clip.title)
        .join(Clip, Clip.id == Post.clip_id)
        .where(Post.project_id == project.id)
        .order_by(Post.created_at.desc())
    ).all()
    total = sum(p.views or 0 for p, _ in rows)
    cpm = load_settings(project.settings).cpm_usd
    return PostsSummary(
        posts=[PostOut.build(p, title) for p, title in rows],
        total_views=total,
        est_revenue_usd=round(total / 1000 * cpm, 2) if cpm is not None else None,
    )


@router.post("/clips/{clip_id}/posts", response_model=PostOut, status_code=201)
def add_post(
    clip_id: uuid.UUID, body: PostIn, user: CurrentUser = Depends(current_user), db: Session = Depends(get_db)
) -> PostOut:
    clip = scoped(db, Clip, clip_id, user)
    try:
        url = parse_media_url(body.url)
    except UnsafeURLError as exc:
        raise HTTPException(422, str(exc)) from exc
    post = Post(
        workspace_id=clip.workspace_id,
        project_id=clip.project_id,
        clip_id=clip.id,
        platform=detect_platform(url),
        url=url,
        created_by=user.id,
    )
    db.add(post)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "That post is already being tracked.") from exc
    enqueue(db, "posts.refresh", {"post_ids": [str(post.id)]}, max_attempts=1, workspace_id=clip.workspace_id)
    db.commit()
    return PostOut.build(post, clip.title)


@router.post("/posts/{post_id}/refresh", status_code=202)
def refresh_post(
    post_id: uuid.UUID, user: CurrentUser = Depends(current_user), db: Session = Depends(get_db)
) -> dict:
    post = scoped(db, Post, post_id, user)
    enqueue(
        db,
        "posts.refresh",
        {"post_ids": [str(post.id)]},
        max_attempts=1,
        dedupe_key=f"post-refresh:{post.id}",
        workspace_id=post.workspace_id,
    )
    db.commit()
    return {"queued": True}


@router.delete("/posts/{post_id}", status_code=204)
def delete_post(
    post_id: uuid.UUID, user: CurrentUser = Depends(current_user), db: Session = Depends(get_db)
) -> None:
    db.delete(scoped(db, Post, post_id, user))
    db.commit()
