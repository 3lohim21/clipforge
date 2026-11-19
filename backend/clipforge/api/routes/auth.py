from __future__ import annotations

import threading
import time
import uuid
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from clipforge.api.deps import COOKIE_NAME, CurrentUser, admin_user, auth_cache, current_user, get_db
from clipforge.api.schemas import LoginIn, MeOut, PasswordChangeIn, UserIn, UserOut
from clipforge.config import get_settings
from clipforge.core.security import hash_password, hash_token, verify_password
from clipforge.db.models import User, UserSession, Workspace
from clipforge.services.users import authenticate, start_session

router = APIRouter(prefix="/api", tags=["auth"])

# Failed-login throttle per client IP: 10 failures / 15 min. Per-process; for multiple API
# replicas put the same limit on the reverse proxy (see docs/DEPLOYMENT.md).
_FAILS: dict[str, deque[float]] = defaultdict(deque)
_FAILS_LOCK = threading.Lock()
_WINDOW_S, _MAX_FAILS = 15 * 60, 10


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _throttled(ip: str) -> bool:
    now = time.monotonic()
    with _FAILS_LOCK:
        q = _FAILS[ip]
        while q and now - q[0] > _WINDOW_S:
            q.popleft()
        return len(q) >= _MAX_FAILS


def _record_failure(ip: str) -> None:
    with _FAILS_LOCK:
        _FAILS[ip].append(time.monotonic())


def _me(db: Session, user_id) -> MeOut:  # type: ignore[no-untyped-def]
    user = db.get(User, user_id)
    assert user is not None
    workspace = db.get(Workspace, user.workspace_id)
    return MeOut(
        user=UserOut.model_validate(user), workspace={"id": str(workspace.id), "name": workspace.name}
    )


@router.post("/auth/login", response_model=MeOut)
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)) -> MeOut:
    ip = _client_ip(request)
    if _throttled(ip):
        raise HTTPException(429, "Too many failed attempts. Try again in a few minutes.")
    user = authenticate(db, body.email, body.password)
    if user is None:
        _record_failure(ip)
        raise HTTPException(401, "Wrong email or password.")
    settings = get_settings()
    token = start_session(db, user, settings, user_agent=request.headers.get("user-agent"), ip=ip)
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=settings.session_ttl_days * 86400,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )
    return _me(db, user.id)


@router.post("/auth/logout", status_code=204)
def logout(request: Request, response: Response, db: Session = Depends(get_db)) -> None:
    token = request.cookies.get(COOKIE_NAME)
    if token:
        key = hash_token(token)
        db.execute(delete(UserSession).where(UserSession.token_hash == key))
        db.commit()
        auth_cache.evict(key)
    response.delete_cookie(COOKIE_NAME, path="/")


@router.get("/auth/me", response_model=MeOut)
def me(user: CurrentUser = Depends(current_user), db: Session = Depends(get_db)) -> MeOut:
    return _me(db, user.id)


@router.post("/auth/password", status_code=204)
def change_password(
    body: PasswordChangeIn,
    request: Request,
    user: CurrentUser = Depends(current_user),
    db: Session = Depends(get_db),
) -> None:
    row = db.get(User, user.id)
    assert row is not None
    if not verify_password(body.current_password, row.password_hash):
        raise HTTPException(400, "Current password is incorrect.")
    row.password_hash = hash_password(body.new_password)
    # Sign out every other device.
    current = hash_token(request.cookies.get(COOKIE_NAME, ""))
    db.execute(delete(UserSession).where(UserSession.user_id == user.id, UserSession.token_hash != current))
    db.commit()
    auth_cache.evict_user(user.id)


# --- team ------------------------------------------------------------------------------------


@router.get("/users", response_model=list[UserOut], tags=["team"])
def list_users(user: CurrentUser = Depends(current_user), db: Session = Depends(get_db)) -> list[User]:
    return list(
        db.scalars(select(User).where(User.workspace_id == user.workspace_id).order_by(User.created_at))
    )


@router.post("/users", response_model=UserOut, status_code=201, tags=["team"])
def create_user(
    body: UserIn, admin: CurrentUser = Depends(admin_user), db: Session = Depends(get_db)
) -> User:
    if db.scalar(select(func.count()).select_from(User).where(func.lower(User.email) == body.email)):
        raise HTTPException(409, "A user with that email already exists.")
    user = User(
        workspace_id=admin.workspace_id,
        email=body.email,
        name=body.name.strip(),
        password_hash=hash_password(body.password),
        role=body.role,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "A user with that email already exists.") from exc
    return user


@router.delete("/users/{user_id}", status_code=204, tags=["team"])
def delete_user(
    user_id: uuid.UUID, admin: CurrentUser = Depends(admin_user), db: Session = Depends(get_db)
) -> None:
    target = db.get(User, user_id)
    if target is None or target.workspace_id != admin.workspace_id:
        raise HTTPException(404, "User not found")
    if target.id == admin.id or target.role == "owner":
        raise HTTPException(400, "You can't remove yourself or the workspace owner.")
    db.delete(target)
    db.commit()
    auth_cache.evict_user(target.id)
