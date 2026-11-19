"""Request-scoped dependencies: DB session, authenticated user, role checks.

Sessions are opaque random tokens in an HttpOnly cookie; the DB stores only their SHA-256.
A 30-second in-process cache keeps authenticated media streaming (a video player issues
dozens of Range requests per minute) from hitting the database on every request.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from clipforge.config import get_settings
from clipforge.core.security import hash_token
from clipforge.db import session_factory
from clipforge.db.models import User, UserSession

COOKIE_NAME = "cf_session"
_CACHE_TTL_S = 30.0
_TOUCH_EVERY = timedelta(minutes=10)


@dataclass(frozen=True)
class CurrentUser:
    id: uuid.UUID
    workspace_id: uuid.UUID
    email: str
    name: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role in ("owner", "admin")


class _AuthCache:
    def __init__(self, max_entries: int = 10_000) -> None:
        self._data: dict[bytes, tuple[float, CurrentUser]] = {}
        self._lock = threading.Lock()
        self._max = max_entries

    def get(self, key: bytes) -> CurrentUser | None:
        with self._lock:
            hit = self._data.get(key)
            if hit and hit[0] > time.monotonic():
                return hit[1]
            self._data.pop(key, None)
            return None

    def put(self, key: bytes, user: CurrentUser) -> None:
        with self._lock:
            if len(self._data) >= self._max:
                self._data.clear()  # crude but bounded; entries are cheap to rebuild
            self._data[key] = (time.monotonic() + _CACHE_TTL_S, user)

    def evict(self, key: bytes) -> None:
        with self._lock:
            self._data.pop(key, None)

    def evict_user(self, user_id: uuid.UUID) -> None:
        with self._lock:
            for k in [k for k, (_, u) in self._data.items() if u.id == user_id]:
                del self._data[k]


auth_cache = _AuthCache()


def get_db() -> Iterator[Session]:
    """Routes commit explicitly; anything uncommitted is rolled back on exit."""
    session = session_factory()()
    try:
        yield session
    finally:
        session.close()


def _unauthorized() -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in")


def current_user(request: Request, db: Session = Depends(get_db)) -> CurrentUser:
    token = request.cookies.get(COOKIE_NAME)
    if not token or len(token) > 200:
        raise _unauthorized()
    key = hash_token(token)
    cached = auth_cache.get(key)
    if cached:
        return cached
    now = datetime.now(UTC)
    row = db.execute(
        select(User, UserSession)
        .join(UserSession, UserSession.user_id == User.id)
        .where(UserSession.token_hash == key, UserSession.expires_at > now, User.is_active.is_(True))
    ).first()
    if row is None:
        raise _unauthorized()
    user, sess = row
    if now - sess.last_seen_at > _TOUCH_EVERY:  # sliding expiry, written at most every 10 min
        db.execute(
            update(UserSession)
            .where(UserSession.id == sess.id)
            .values(last_seen_at=now, expires_at=now + timedelta(days=get_settings().session_ttl_days))
        )
        db.commit()
    result = CurrentUser(user.id, user.workspace_id, user.email, user.name, user.role)
    auth_cache.put(key, result)
    return result


def admin_user(user: CurrentUser = Depends(current_user)) -> CurrentUser:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admins only")
    return user
