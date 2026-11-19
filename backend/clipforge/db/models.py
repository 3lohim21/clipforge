"""ORM mappings. The SQL migrations are authoritative; these classes mirror them.

JSONB columns are treated as immutable values: services build a new dict and assign it,
which keeps change tracking explicit and avoids MutableDict surprises.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, BYTEA, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from clipforge.core.ids import new_id

JsonDict = dict[str, Any]


class Base(DeclarativeBase):
    # Fetch server-generated values (created_at/updated_at) in the INSERT/UPDATE itself via
    # RETURNING, instead of a lazy SELECT per row on first access.
    __mapper_args__ = {"eager_defaults": True}
    type_annotation_map = {
        datetime: DateTime(timezone=True),
        JsonDict: JSONB,
        bytes: BYTEA,
        str: Text,
    }


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(primary_key=True, default=new_id)


def _created() -> Mapped[datetime]:
    return mapped_column(server_default=func.now())


def _updated() -> Mapped[datetime]:
    return mapped_column(server_default=func.now(), onupdate=func.now())


def _fk(target: str, *, nullable: bool = False, ondelete: str = "CASCADE") -> Any:
    return mapped_column(ForeignKey(target, ondelete=ondelete), nullable=nullable)


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str]
    created_at: Mapped[datetime] = _created()


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = _fk("workspaces.id")
    email: Mapped[str]
    name: Mapped[str] = mapped_column(default="")
    password_hash: Mapped[str]
    role: Mapped[str] = mapped_column(default="editor")
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = _created()
    last_login_at: Mapped[datetime | None]


class UserSession(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = _fk("users.id")
    token_hash: Mapped[bytes]
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime]
    last_seen_at: Mapped[datetime] = _created()
    user_agent: Mapped[str | None]
    ip: Mapped[str | None]


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = _fk("workspaces.id")
    name: Mapped[str]
    settings: Mapped[JsonDict] = mapped_column(default=dict)
    created_by: Mapped[uuid.UUID | None] = _fk("users.id", nullable=True, ondelete="SET NULL")
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class Video(Base):
    __tablename__ = "videos"

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = _fk("workspaces.id")
    project_id: Mapped[uuid.UUID] = _fk("projects.id")
    source_type: Mapped[str]
    source_url: Mapped[str | None]
    import_options: Mapped[JsonDict] = mapped_column(default=dict)
    title: Mapped[str] = mapped_column(default="")
    platform: Mapped[str | None]
    external_id: Mapped[str | None]
    uploader: Mapped[str | None]
    status: Mapped[str] = mapped_column(default="queued")
    progress: Mapped[float] = mapped_column(default=0.0)
    error: Mapped[str | None]
    duration_ms: Mapped[int | None]
    width: Mapped[int | None]
    height: Mapped[int | None]
    fps: Mapped[float | None]
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    has_audio: Mapped[bool | None]
    language: Mapped[str | None]
    transcript_source: Mapped[str | None]
    assets: Mapped[JsonDict] = mapped_column(default=dict)
    meta: Mapped[JsonDict] = mapped_column(default=dict)
    created_by: Mapped[uuid.UUID | None] = _fk("users.id", nullable=True, ondelete="SET NULL")
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class Clip(Base):
    __tablename__ = "clips"

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = _fk("workspaces.id")
    project_id: Mapped[uuid.UUID] = _fk("projects.id")
    video_id: Mapped[uuid.UUID] = _fk("videos.id")
    origin: Mapped[str]
    status: Mapped[str] = mapped_column(default="suggested")
    start_ms: Mapped[int]
    end_ms: Mapped[int]
    title: Mapped[str] = mapped_column(default="")
    hook: Mapped[str | None]
    reason: Mapped[str | None]
    hashtags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    score: Mapped[int | None]
    score_breakdown: Mapped[JsonDict] = mapped_column(default=dict)
    edit: Mapped[JsonDict] = mapped_column(default=dict)
    created_by: Mapped[uuid.UUID | None] = _fk("users.id", nullable=True, ondelete="SET NULL")
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()


class Render(Base):
    __tablename__ = "renders"

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = _fk("workspaces.id")
    project_id: Mapped[uuid.UUID] = _fk("projects.id")
    clip_id: Mapped[uuid.UUID] = _fk("clips.id")
    status: Mapped[str] = mapped_column(default="queued")
    progress: Mapped[float] = mapped_column(default=0.0)
    spec: Mapped[JsonDict]
    output_key: Mapped[str | None]
    thumbnail_key: Mapped[str | None]
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    duration_ms: Mapped[int | None]
    width: Mapped[int | None]
    height: Mapped[int | None]
    error: Mapped[str | None]
    created_by: Mapped[uuid.UUID | None] = _fk("users.id", nullable=True, ondelete="SET NULL")
    created_at: Mapped[datetime] = _created()
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]


class Post(Base):
    __tablename__ = "posts"

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = _fk("workspaces.id")
    project_id: Mapped[uuid.UUID] = _fk("projects.id")
    clip_id: Mapped[uuid.UUID] = _fk("clips.id")
    platform: Mapped[str]
    url: Mapped[str]
    account: Mapped[str | None]
    views: Mapped[int | None] = mapped_column(BigInteger)
    likes: Mapped[int | None] = mapped_column(BigInteger)
    comments: Mapped[int | None] = mapped_column(BigInteger)
    stats_error: Mapped[str | None]
    stats_updated_at: Mapped[datetime | None]
    created_by: Mapped[uuid.UUID | None] = _fk("users.id", nullable=True, ondelete="SET NULL")
    created_at: Mapped[datetime] = _created()


class JobRow(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    queue: Mapped[str]
    kind: Mapped[str]
    payload: Mapped[JsonDict] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(default="queued")
    priority: Mapped[int] = mapped_column(default=0)
    attempts: Mapped[int] = mapped_column(default=0)
    max_attempts: Mapped[int] = mapped_column(default=3)
    run_at: Mapped[datetime] = _created()
    locked_by: Mapped[str | None]
    locked_at: Mapped[datetime | None]
    heartbeat_at: Mapped[datetime | None]
    last_error: Mapped[str | None]
    dedupe_key: Mapped[str | None]
    workspace_id: Mapped[uuid.UUID | None]
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()
    finished_at: Mapped[datetime | None]
