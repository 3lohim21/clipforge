from __future__ import annotations

import time
import uuid
from typing import Any

from sqlalchemy import update

from clipforge.db import session_scope
from clipforge.db.models import Render, Video


def set_video(video_id: uuid.UUID, **values: Any) -> None:
    with session_scope() as s:
        s.execute(update(Video).where(Video.id == video_id).values(**values))


def set_render(render_id: uuid.UUID, **values: Any) -> None:
    with session_scope() as s:
        s.execute(update(Render).where(Render.id == render_id).values(**values))


class Progress:
    """Maps a stage's 0..1 progress into [lo, hi] and writes it at most once per
    `interval` seconds — progress callbacks fire thousands of times; the DB sees ~1/s."""

    def __init__(
        self,
        model: type[Video] | type[Render],
        row_id: uuid.UUID,
        lo: float = 0.0,
        hi: float = 1.0,
        interval: float = 1.0,
    ) -> None:
        self.model, self.row_id, self.lo, self.hi, self.interval = model, row_id, lo, hi, interval
        self._last_t = 0.0
        self._last_v = -1.0

    def __call__(self, fraction: float) -> None:
        value = round(self.lo + (self.hi - self.lo) * max(0.0, min(1.0, fraction)), 4)
        now = time.monotonic()
        if value <= self._last_v or (now - self._last_t < self.interval and fraction < 1.0):
            return
        self._last_t, self._last_v = now, value
        with session_scope() as s:
            s.execute(update(self.model).where(self.model.id == self.row_id).values(progress=value))

    def span(self, lo: float, hi: float) -> Progress:
        """A child reporter covering a sub-range of this one."""
        width = self.hi - self.lo
        child = Progress(self.model, self.row_id, self.lo + width * lo, self.lo + width * hi, self.interval)
        child._last_v = self._last_v
        return child
