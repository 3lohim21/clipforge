"""Per-project settings: the brand kit + clip rules for one client/campaign.

Validated by the API on write and by workers on read, so a hand-edited JSON row can never
crash a pipeline stage (unknown keys are ignored, bad values fall back to defaults).
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from clipforge.rendering.presets import CAPTION_PRESETS, LAYOUTS

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


class ProjectSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # brand kit
    caption_preset: str = "bold"
    layout: str = "vertical_fill"
    highlight_color: str | None = None
    caption_position: float = Field(0.72, ge=0.1, le=0.95)
    title_overlay: bool = False
    watermark: str | None = Field(None, max_length=40)
    # clip rules
    min_clip_s: int = Field(15, ge=5, le=170)
    max_clip_s: int = Field(60, ge=10, le=180)
    target_clip_s: int = Field(35, ge=5, le=180)
    max_clips: int | None = Field(None, ge=1, le=50)
    # processing
    language: str | None = Field(None, max_length=8)
    transcript_source: Literal["auto", "whisper"] = "auto"
    ai_rerank: bool = True
    # business: campaign payout per 1,000 views, for revenue estimates
    cpm_usd: float | None = Field(None, ge=0, le=1000)

    @field_validator("caption_preset")
    @classmethod
    def _preset(cls, v: str) -> str:
        return v if v in CAPTION_PRESETS else "bold"

    @field_validator("layout")
    @classmethod
    def _layout(cls, v: str) -> str:
        return v if v in LAYOUTS else "vertical_fill"

    @field_validator("highlight_color")
    @classmethod
    def _color(cls, v: str | None) -> str | None:
        return v if v and _HEX.match(v) else None

    @field_validator("watermark", "language")
    @classmethod
    def _blank_to_none(cls, v: str | None) -> str | None:
        return v.strip() or None if v else None

    @model_validator(mode="after")
    def _lengths(self) -> ProjectSettings:
        if self.max_clip_s <= self.min_clip_s:
            self.max_clip_s = min(180, self.min_clip_s + 10)
        self.target_clip_s = min(max(self.target_clip_s, self.min_clip_s), self.max_clip_s)
        return self


def load_settings(raw: dict | None) -> ProjectSettings:
    try:
        return ProjectSettings.model_validate(raw or {})
    except ValueError:
        return ProjectSettings()
