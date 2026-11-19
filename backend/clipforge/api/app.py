"""ASGI application factory."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from clipforge import __version__
from clipforge.api.routes import auth, clips, meta, posts, projects, videos
from clipforge.config import Settings, get_settings
from clipforge.db import dispose_engine, get_engine, session_factory
from clipforge.db.migrate import migrate
from clipforge.logging import configure_logging
from clipforge.metrics import HTTP_REQUESTS, HTTP_SECONDS
from clipforge.services.users import bootstrap_admin
from clipforge.storage import StorageError

log = logging.getLogger("clipforge.http")
_UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


class ObservabilityMiddleware:
    """Pure ASGI (no BaseHTTPMiddleware): adds no buffering to streamed video responses.
    Records latency/status per route template and blocks cross-site writes (CSRF defence
    in depth on top of SameSite=Lax cookies)."""

    def __init__(self, app: ASGIApp, allowed_origins: set[str]) -> None:
        self.app = app
        self.allowed_origins = allowed_origins

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method = scope["method"]
        if method in _UNSAFE:
            headers = dict(scope.get("headers") or [])
            origin = headers.get(b"origin", b"").decode()
            host = headers.get(b"host", b"").decode()
            if origin and urlsplit(origin).netloc != host and origin not in self.allowed_origins:
                response = JSONResponse({"detail": "Cross-origin request blocked"}, status_code=403)
                await response(scope, receive, send)
                return

        started = time.perf_counter()
        status_holder = {"code": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            route = scope.get("route")
            label = getattr(route, "path", None) or "unmatched"
            elapsed = time.perf_counter() - started
            HTTP_REQUESTS.labels(method, label, str(status_holder["code"])).inc()
            HTTP_SECONDS.labels(method, label).observe(elapsed)
            if elapsed > 1.0 and not label.endswith(("/upload", "/media/{kind}", "/file")):
                log.warning(
                    "slow request", extra={"route": label, "method": method, "ms": int(elapsed * 1000)}
                )


def _mount_frontend(app: FastAPI, dist: Path) -> None:
    index = dist / "index.html"
    if (dist / "assets").is_dir():
        # Vite emits content-hashed file names, so these can be cached forever.
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        if path.startswith(("api/", "metrics")):
            raise HTTPException(404, "Not found")
        candidate = (dist / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(dist.resolve()):
            return FileResponse(candidate)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        applied = migrate(get_engine())
        if applied:
            log.info("migrations applied", extra={"versions": ",".join(applied)})
        db = session_factory()()
        try:
            bootstrap_admin(db, settings)
        finally:
            db.close()
        settings.media_dir.mkdir(parents=True, exist_ok=True)
        yield
        dispose_engine()

    app = FastAPI(
        title="ClipForge API",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    public_origin = "{0.scheme}://{0.netloc}".format(urlsplit(settings.public_url))
    app.add_middleware(ObservabilityMiddleware, allowed_origins={public_origin, *settings.cors_origins})
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.exception_handler(StorageError)
    async def _storage_error(_: Request, __: StorageError) -> JSONResponse:
        return JSONResponse({"detail": "Not found"}, status_code=404)

    for module in (auth, projects, videos, clips, posts, meta):
        app.include_router(module.router)

    if settings.frontend_dist and (settings.frontend_dist / "index.html").is_file():
        _mount_frontend(app, settings.frontend_dist)
    return app
