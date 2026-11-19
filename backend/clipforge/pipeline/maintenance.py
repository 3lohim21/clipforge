"""Maintenance jobs: deleting media for removed rows, and refreshing view counts of posted
clips (campaigns pay per 1,000 views, so this is the agency's revenue signal)."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select

from clipforge.core.urls import assert_public_host
from clipforge.db import session_scope
from clipforge.db.models import Post
from clipforge.jobs.queue import enqueue
from clipforge.jobs.worker import JobContext
from clipforge.storage import get_storage

log = logging.getLogger(__name__)

STATS_TTL = timedelta(hours=6)


def cleanup(ctx: JobContext) -> None:
    storage = get_storage()
    for prefix in ctx.payload.get("prefixes", []):
        storage.delete_prefix(prefix)


def fetch_post_stats(url: str, settings) -> dict:  # type: ignore[no-untyped-def]
    from yt_dlp import YoutubeDL

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 20,
        "extractor_retries": 2,
    }
    if settings.ytdlp_cookies_file:
        opts["cookiefile"] = str(settings.ytdlp_cookies_file)
    if settings.ytdlp_proxy:
        opts["proxy"] = settings.ytdlp_proxy
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False, process=False) or {}
    return {
        "views": info.get("view_count"),
        "likes": info.get("like_count"),
        "comments": info.get("comment_count"),
        "account": info.get("uploader") or info.get("channel") or info.get("uploader_id"),
    }


def refresh_posts(ctx: JobContext) -> None:
    ids = [uuid.UUID(i) for i in ctx.payload.get("post_ids", [])]
    with session_scope() as s:
        query = select(Post.id, Post.url)
        if ids:
            query = query.where(Post.id.in_(ids))
        else:
            cutoff = datetime.now(UTC) - STATS_TTL
            query = (
                query.where(or_(Post.stats_updated_at.is_(None), Post.stats_updated_at < cutoff))
                .order_by(Post.stats_updated_at.asc().nulls_first())
                .limit(50)
            )
        targets = list(s.execute(query))
    for post_id, url in targets:
        if ctx.should_stop():
            break
        values: dict = {"stats_updated_at": datetime.now(UTC)}
        try:
            assert_public_host(url, allow_private=ctx.settings.allow_private_urls)
            stats = fetch_post_stats(url, ctx.settings)
            values |= {k: v for k, v in stats.items() if v is not None} | {"stats_error": None}
        except Exception as exc:  # one bad link must not stop the batch
            values["stats_error"] = str(exc)[:300]
        with session_scope() as s:
            post = s.get(Post, post_id)
            if post is not None:
                for key, value in values.items():
                    setattr(post, key, value)


def schedule_stats_refresh() -> None:
    """Periodic hook (runs in workers serving `maintenance`): queue a refresh when any post's
    stats are stale. The dedupe key keeps it to one pending refresh at a time."""
    cutoff = datetime.now(UTC) - STATS_TTL
    with session_scope() as s:
        stale = s.scalar(
            select(Post.id)
            .where(or_(Post.stats_updated_at.is_(None), Post.stats_updated_at < cutoff))
            .limit(1)
        )
        if stale is not None:
            enqueue(s, "posts.refresh", {}, dedupe_key="posts-refresh", max_attempts=1)
