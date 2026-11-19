from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from clipforge.config import Settings
from clipforge.core.security import (
    dummy_password_hash,
    hash_password,
    hash_token,
    new_session_token,
    verify_password,
)
from clipforge.db.models import User, UserSession, Workspace

log = logging.getLogger(__name__)


def bootstrap_admin(db: Session, settings: Settings) -> None:
    """First boot: create the agency workspace + owner from ADMIN_EMAIL / ADMIN_PASSWORD.
    Never touches an existing install (won't reset passwords)."""
    if not settings.admin_email or not settings.admin_password:
        return
    db.execute(text("SELECT pg_advisory_xact_lock(7270431002)"))  # one bootstrapper at a time
    if db.scalar(select(func.count()).select_from(User)):
        db.commit()
        return
    workspace = Workspace(name="My Agency")
    db.add(workspace)
    db.flush()
    db.add(
        User(
            workspace_id=workspace.id,
            email=settings.admin_email.strip().lower(),
            name="Owner",
            password_hash=hash_password(settings.admin_password),
            role="owner",
        )
    )
    db.commit()
    log.info("bootstrapped owner account", extra={"email": settings.admin_email})


def authenticate(db: Session, email: str, password: str) -> User | None:
    user = db.scalar(select(User).where(func.lower(User.email) == email.strip().lower()))
    if user is None or not user.is_active:
        verify_password(password, dummy_password_hash())  # equalise timing
        return None
    return user if verify_password(password, user.password_hash) else None


def start_session(
    db: Session, user: User, settings: Settings, *, user_agent: str | None, ip: str | None
) -> str:
    token = new_session_token()
    now = datetime.now(UTC)
    db.add(
        UserSession(
            user_id=user.id,
            token_hash=hash_token(token),
            expires_at=now + timedelta(days=settings.session_ttl_days),
            user_agent=(user_agent or "")[:300],
            ip=ip,
        )
    )
    user.last_login_at = now
    # Opportunistic housekeeping: drop this user's expired sessions.
    db.execute(delete(UserSession).where(UserSession.user_id == user.id, UserSession.expires_at < now))
    db.commit()
    return token
