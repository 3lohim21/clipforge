"""Request/response contracts. Responses never expose storage keys or internal paths — media
is addressed through authenticated URLs built here."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from clipforge.core.project_settings import ProjectSettings
from clipforge.db.models import Clip, Post, Render, Video
from clipforge.rendering.presets import CAPTION_PRESETS, LAYOUTS


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- auth / users -------------------------------------------------------------------------


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=200)


class PasswordChangeIn(BaseModel):
    current_password: str = Field(max_length=200)
    new_password: str = Field(min_length=10, max_length=200)


class UserOut(_Out):
    id: uuid.UUID
    email: str
    name: str
    role: str
    created_at: datetime
    last_login_at: datetime | None = None


class UserIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    name: str = Field("", max_length=120)
    password: str = Field(min_length=10, max_length=200)
    role: Literal["admin", "editor"] = "editor"

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip().lower()
        if "@" not in v or v.startswith("@") or v.endswith("@"):
            raise ValueError("invalid email")
        return v


class MeOut(BaseModel):
    user: UserOut
    workspace: dict[str, Any]


# --- projects -------------------------------------------------------------------------------


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    settings: ProjectSettings = Field(default_factory=ProjectSettings)


class ProjectPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    settings: ProjectSettings | None = None


class ProjectStats(BaseModel):
    videos: int = 0
    clips_saved: int = 0
    clips_suggested: int = 0
    renders: int = 0
    posts: int = 0
    views: int = 0
    est_revenue_usd: float | None = None


class ProjectOut(_Out):
    id: uuid.UUID
    name: str
    settings: ProjectSettings
    created_at: datetime
    updated_at: datetime
    stats: ProjectStats = Field(default_factory=ProjectStats)


# --- videos -----------------------------------------------------------------------------------


class ImportIn(BaseModel):
    urls: list[str] = Field(default_factory=list, max_length=50)
    text: str | None = Field(None, max_length=20_000)  # pasted blob: one link per line
    section_start_s: float | None = Field(None, ge=0)
    section_end_s: float | None = Field(None, gt=0)


class VideoOut(_Out):
    id: uuid.UUID
    project_id: uuid.UUID
    source_type: str
    source_url: str | None
    title: str
    platform: str | None
    uploader: str | None
    status: str
    progress: float
    error: str | None
    duration_ms: int | None
    width: int | None
    height: int | None
    fps: float | None
    has_audio: bool | None
    language: str | None
    transcript_source: str | None
    created_at: datetime
    updated_at: datetime
    poster_url: str | None = None
    playback_url: str | None = None
    has_transcript: bool = False
    has_heatmap: bool = False
    analysis: dict[str, Any] | None = None
    clip_count: int | None = None

    @classmethod
    def build(cls, v: Video, clip_count: int | None = None) -> VideoOut:
        out = cls.model_validate(v)
        assets, meta = v.assets or {}, v.meta or {}
        if assets.get("poster"):
            out.poster_url = f"/api/videos/{v.id}/media/poster?v={int(v.updated_at.timestamp())}"
        if assets.get("playback"):
            out.playback_url = f"/api/videos/{v.id}/media/playback"
        out.has_transcript = bool(assets.get("transcript"))
        out.has_heatmap = bool(meta.get("has_heatmap"))
        out.analysis = meta.get("analysis")
        out.clip_count = clip_count
        return out


# --- clips ------------------------------------------------------------------------------------


class ClipEdit(BaseModel):
    model_config = ConfigDict(extra="ignore")

    layout: str = "vertical_fill"
    captions: bool = True
    caption_preset: str = "bold"
    caption_position: float = Field(0.72, ge=0.1, le=0.95)
    highlight_color: str | None = Field(None, pattern=r"^#[0-9a-fA-F]{6}$")
    title_overlay: bool = False
    reframe: Literal["auto", "center", "manual"] = "auto"
    crop_x: float | None = Field(None, ge=0, le=1)

    @field_validator("layout")
    @classmethod
    def _layout(cls, v: str) -> str:
        if v not in LAYOUTS:
            raise ValueError(f"unknown layout {v!r}")
        return v

    @field_validator("caption_preset")
    @classmethod
    def _preset(cls, v: str) -> str:
        if v not in CAPTION_PRESETS:
            raise ValueError(f"unknown caption preset {v!r}")
        return v


class ClipIn(BaseModel):
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    title: str = Field("", max_length=200)
    edit: ClipEdit | None = None


class ClipPatch(BaseModel):
    start_ms: int | None = Field(None, ge=0)
    end_ms: int | None = Field(None, gt=0)
    title: str | None = Field(None, max_length=200)
    status: Literal["suggested", "saved", "discarded"] | None = None
    hashtags: list[str] | None = Field(None, max_length=15)
    edit: ClipEdit | None = None


class RenderOut(_Out):
    id: uuid.UUID
    clip_id: uuid.UUID
    status: str
    progress: float
    error: str | None
    width: int | None
    height: int | None
    duration_ms: int | None
    size_bytes: int | None
    created_at: datetime
    finished_at: datetime | None
    layout: str | None = None
    file_url: str | None = None
    download_url: str | None = None
    thumbnail_url: str | None = None
    clip_title: str | None = None

    @classmethod
    def build(cls, r: Render, clip_title: str | None = None) -> RenderOut:
        out = cls.model_validate(r)
        out.layout = (r.spec or {}).get("layout")
        if r.status == "done" and r.output_key:
            out.file_url = f"/api/renders/{r.id}/file"
            out.download_url = f"/api/renders/{r.id}/file?download=1"
            out.thumbnail_url = f"/api/renders/{r.id}/thumbnail"
        out.clip_title = clip_title
        return out


class ClipOut(_Out):
    id: uuid.UUID
    video_id: uuid.UUID
    project_id: uuid.UUID
    origin: str
    status: str
    start_ms: int
    end_ms: int
    title: str
    hook: str | None
    reason: str | None
    hashtags: list[str]
    score: int | None
    score_breakdown: dict[str, Any]
    edit: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    latest_render: RenderOut | None = None
    video_title: str | None = None

    @classmethod
    def build(cls, c: Clip, latest: Render | None = None, video_title: str | None = None) -> ClipOut:
        out = cls.model_validate(c)
        out.latest_render = RenderOut.build(latest) if latest else None
        out.video_title = video_title
        return out


class RenderIn(BaseModel):
    """Optional one-off overrides on top of the clip's saved edit settings."""

    edit: ClipEdit | None = None


# --- posts --------------------------------------------------------------------------------------


class PostIn(BaseModel):
    url: str = Field(min_length=8, max_length=2048)


class PostOut(_Out):
    id: uuid.UUID
    clip_id: uuid.UUID
    platform: str
    url: str
    account: str | None
    views: int | None
    likes: int | None
    comments: int | None
    stats_error: str | None
    stats_updated_at: datetime | None
    created_at: datetime
    clip_title: str | None = None

    @classmethod
    def build(cls, p: Post, clip_title: str | None = None) -> PostOut:
        out = cls.model_validate(p)
        out.clip_title = clip_title
        return out


class PostsSummary(BaseModel):
    posts: list[PostOut]
    total_views: int
    est_revenue_usd: float | None
