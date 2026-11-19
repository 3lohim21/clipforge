from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Gauge, generate_latest
from prometheus_client import REGISTRY as DEFAULT_REGISTRY
from sqlalchemy import text

from clipforge import __version__
from clipforge.api.deps import CurrentUser, current_user
from clipforge.config import FONTS_DIR, get_settings
from clipforge.db import get_engine
from clipforge.jobs.queue import queue_depths
from clipforge.rendering.presets import catalog

router = APIRouter(tags=["meta"])
log = logging.getLogger(__name__)

_FONT_FILES = {p.name for p in FONTS_DIR.glob("*.ttf")}


@router.get("/api/presets")
def presets(_: CurrentUser = Depends(current_user)) -> dict:
    return catalog()


@router.get("/api/fonts/{name}")
def font(name: str) -> FileResponse:
    if name not in _FONT_FILES:  # allow-list: no path traversal possible
        raise HTTPException(404, "Font not found")
    return FileResponse(
        FONTS_DIR / name,
        media_type="font/ttf",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@router.get("/api/healthz")
def healthz() -> dict:
    """Liveness: the process is up. Never touches dependencies."""
    return {"ok": True, "version": __version__}


@router.get("/api/readyz")
def readyz() -> dict:
    """Readiness: can serve traffic (database reachable)."""
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(503, "database unavailable") from exc
    return {"ok": True}


@router.get("/metrics", include_in_schema=False)
def metrics(request: Request) -> Response:
    token = get_settings().metrics_token
    if token:
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not secrets.compare_digest(supplied, token):
            raise HTTPException(401, "metrics token required")
    # Queue depth / age are the autoscaling signals for worker pools (KEDA, HPA, or a cron).
    registry = CollectorRegistry()
    depth = Gauge("clipforge_queue_jobs", "Jobs by queue and status", ["queue", "status"], registry=registry)
    oldest = Gauge(
        "clipforge_queue_oldest_seconds", "Age of the oldest job", ["queue", "status"], registry=registry
    )
    try:
        for row in queue_depths(get_engine()):
            depth.labels(row["queue"], row["status"]).set(row["n"])
            oldest.labels(row["queue"], row["status"]).set(float(row["oldest_s"] or 0))
    except Exception:  # metrics must not fail because the DB is down
        log.debug("queue depth unavailable", exc_info=True)
    body = generate_latest(DEFAULT_REGISTRY) + generate_latest(registry)
    return Response(body, media_type=CONTENT_TYPE_LATEST)
