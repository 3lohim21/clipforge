from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from clipforge.api.deps import CurrentUser, current_user, get_db
from clipforge.api.schemas import ProjectIn, ProjectOut, ProjectPatch
from clipforge.db.models import Project, Video
from clipforge.services.access import scoped
from clipforge.services.projects import (
    media_prefixes_for_videos,
    project_stats,
    schedule_media_cleanup,
    to_out,
)

router = APIRouter(prefix="/api/projects", tags=["projects"])


@router.get("", response_model=list[ProjectOut])
def list_projects(
    user: CurrentUser = Depends(current_user), db: Session = Depends(get_db)
) -> list[ProjectOut]:
    projects = list(
        db.scalars(
            select(Project)
            .where(Project.workspace_id == user.workspace_id)
            .order_by(Project.created_at.desc())
        )
    )
    stats = project_stats(db, [p.id for p in projects])
    return [to_out(p, stats[p.id]) for p in projects]


@router.post("", response_model=ProjectOut, status_code=201)
def create_project(
    body: ProjectIn, user: CurrentUser = Depends(current_user), db: Session = Depends(get_db)
) -> ProjectOut:
    project = Project(
        workspace_id=user.workspace_id,
        name=body.name.strip(),
        settings=body.settings.model_dump(),
        created_by=user.id,
    )
    db.add(project)
    db.commit()
    return to_out(project, project_stats(db, [project.id])[project.id])


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(
    project_id: uuid.UUID, user: CurrentUser = Depends(current_user), db: Session = Depends(get_db)
) -> ProjectOut:
    project = scoped(db, Project, project_id, user)
    return to_out(project, project_stats(db, [project.id])[project.id])


@router.patch("/{project_id}", response_model=ProjectOut)
def update_project(
    project_id: uuid.UUID,
    body: ProjectPatch,
    user: CurrentUser = Depends(current_user),
    db: Session = Depends(get_db),
) -> ProjectOut:
    project = scoped(db, Project, project_id, user)
    if body.name is not None:
        project.name = body.name.strip()
    if body.settings is not None:
        project.settings = body.settings.model_dump()
    db.commit()
    return to_out(project, project_stats(db, [project.id])[project.id])


@router.delete("/{project_id}", status_code=204)
def delete_project(
    project_id: uuid.UUID, user: CurrentUser = Depends(current_user), db: Session = Depends(get_db)
) -> None:
    project = scoped(db, Project, project_id, user)
    video_ids = list(db.scalars(select(Video.id).where(Video.project_id == project.id)))
    schedule_media_cleanup(db, media_prefixes_for_videos(db, video_ids), user.workspace_id)
    db.delete(project)  # cascades to videos, clips, renders, posts
    db.commit()
