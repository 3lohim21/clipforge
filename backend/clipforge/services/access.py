"""Tenant-scoped lookups. Every entity fetch goes through here, so a user can only ever see
rows in their own workspace — a wrong-workspace ID is indistinguishable from a missing one."""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy.orm import Session

from clipforge.api.deps import CurrentUser
from clipforge.db.models import Clip, Post, Project, Render, Video


def scoped[T: (Project, Video, Clip, Render, Post)](
    db: Session, model: type[T], entity_id: uuid.UUID, user: CurrentUser, *, lock: bool = False
) -> T:
    obj = db.get(model, entity_id, with_for_update=lock)
    if obj is None or obj.workspace_id != user.workspace_id:
        raise HTTPException(404, f"{model.__name__} not found")
    return obj
