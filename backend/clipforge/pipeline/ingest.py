"""Stage 1: get the media, make it streamable, extract audio/loudness, and reuse platform
captions when available."""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path
from typing import Any

from clipforge.core.urls import assert_public_host
from clipforge.db import session_scope
from clipforge.db.models import Project, Video
from clipforge.jobs.queue import PermanentJobError, enqueue
from clipforge.jobs.worker import JobContext
from clipforge.media import downloader
from clipforge.media.audio import FRAME_MS, energy_to_u8_b64, extract_audio_with_energy
from clipforge.media.ffmpeg import MediaError, make_poster, mp4_is_faststart, probe, run_ffmpeg
from clipforge.pipeline.common import Progress, set_video
from clipforge.storage import get_storage, video_key, video_prefix
from clipforge.transcription.words import Word, transcript_to_json

log = logging.getLogger(__name__)


def _shift_words(words: list[Word], offset_ms: int, duration_ms: int) -> list[Word]:
    if not offset_ms:
        return words
    out = []
    for w in words:
        start, end = w.start - offset_ms, w.end - offset_ms
        if end > 0 and start < duration_ms:
            out.append(Word(max(0, start), end, w.text, w.prob))
    return out


def _shift_spans(items: list[Any], offset_ms: int, duration_ms: int, kind: str) -> list[Any]:
    out: list[Any] = []
    for item in items:
        if kind == "heatmap":
            s, e, v = item
            if e - offset_ms > 0 and s - offset_ms < duration_ms:
                out.append([max(0, s - offset_ms), min(duration_ms, e - offset_ms), v])
        else:
            s, e = item["start_ms"] - offset_ms, item["end_ms"] - offset_ms
            if e > 0 and s < duration_ms:
                out.append({**item, "start_ms": max(0, s), "end_ms": min(duration_ms, e)})
    return out


def _make_playable(src: Path, vdir: Path, browser_ok: bool, duration_s: float, progress: Progress) -> Path:
    """Return a file the browser can stream with instant seeking."""
    if browser_ok:
        if src.suffix.lower() == ".mp4" and mp4_is_faststart(src):
            return src
        # Same codecs, wrong layout/container: a stream copy (seconds, no re-encode).
        target, tmp = vdir / "playback.mp4", vdir / "playback.tmp.mp4"
        run_ffmpeg(
            [
                "-i",
                str(src),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(tmp),
            ]
        )
        os.replace(tmp, target)
        return target
    # Exotic codec (VP9/AV1/HEVC/ProRes...): a light 720p H.264 proxy for the editor only.
    # Renders still read the full-quality original.
    target, tmp = vdir / "playback.mp4", vdir / "playback.tmp.mp4"
    run_ffmpeg(
        [
            "-i",
            str(src),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-vf",
            "scale=-2:'min(720,ih)'",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "26",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            str(tmp),
        ],
        duration_s=duration_s,
        on_progress=progress,
    )
    os.replace(tmp, target)
    return target


def run(ctx: JobContext) -> None:
    video_id = uuid.UUID(ctx.payload["video_id"])
    settings, storage = ctx.settings, get_storage()
    with session_scope() as s:
        video = s.get(Video, video_id)
        if video is None:
            return  # deleted while queued
        project = s.get(Project, video.project_id)
        project_settings: dict[str, Any] = dict(project.settings) if project else {}
        source_type, source_url = video.source_type, video.source_url
        options, assets = dict(video.import_options), dict(video.assets)

    vdir = storage.path(video_prefix(video_id))
    vdir.mkdir(parents=True, exist_ok=True)
    progress = Progress(Video, video_id)
    info: dict[str, Any] = {}
    caption_words: list[Word] | None = None
    caption_lang: str | None = None
    offset_ms = 0
    updates: dict[str, Any] = {}

    if source_type == "url":
        assert source_url
        set_video(video_id, status="downloading", progress=0.0, error=None)
        try:
            assert_public_host(source_url, allow_private=settings.allow_private_urls)
        except ValueError as exc:
            raise PermanentJobError(str(exc)) from exc
        section = options.get("section")
        want_captions = (
            settings.prefer_platform_captions and project_settings.get("transcript_source") != "whisper"
        )
        result = downloader.download(
            source_url,
            vdir,
            settings=settings,
            on_progress=progress,
            should_stop=ctx.should_stop,
            section=tuple(section) if section else None,
            want_captions=want_captions,
        )
        src, info = result.path, result.info
        caption_words, caption_lang, offset_ms = (
            result.caption_words,
            result.caption_language,
            result.section_offset_ms,
        )
        assets["source"] = video_key(video_id, src.name)
        updates.update(
            title=(info.get("title") or "Untitled")[:300],
            uploader=info.get("uploader") or info.get("channel"),
            platform=(info.get("extractor_key") or "web").lower(),
            external_id=info.get("id"),
        )
    else:
        if not storage.exists(assets.get("source")):
            raise PermanentJobError("The uploaded file is missing. Please upload it again.")
        src = storage.path(assets["source"])

    set_video(video_id, status="processing", progress=0.0, **updates)
    try:
        meta = probe(src)
    except MediaError as exc:
        raise PermanentJobError("This file isn't a readable video.") from exc
    if not meta.has_video or meta.duration_s <= 0:
        raise PermanentJobError("No video stream found in this file.")
    if meta.duration_s > settings.max_source_duration_s:
        raise PermanentJobError(
            f"Video is {meta.duration_s / 3600:.1f} h long; "
            f"the limit is {settings.max_source_duration_s / 3600:.1f} h."
        )
    duration_ms = int(meta.duration_s * 1000)

    playback = _make_playable(src, vdir, meta.browser_playable, meta.duration_s, progress.span(0.0, 0.6))
    assets["playback"] = video_key(video_id, playback.name)
    make_poster(playback, vdir / "poster.jpg", at_s=min(meta.duration_s * 0.1, 30.0))
    assets["poster"] = video_key(video_id, "poster.jpg")

    energy_b64 = None
    if meta.has_audio:
        energy = extract_audio_with_energy(
            src, vdir / "audio.flac", duration_s=meta.duration_s, on_progress=progress.span(0.6, 1.0)
        )
        assets["audio"] = video_key(video_id, "audio.flac")
        energy_b64 = energy_to_u8_b64(energy)

    heatmap = _shift_spans(info.get("heatmap") or [], offset_ms, duration_ms, "heatmap")
    chapters = _shift_spans(info.get("chapters") or [], offset_ms, duration_ms, "chapters")
    storage.write_json(
        video_key(video_id, "timeline.json"),
        {
            "version": 1,
            "duration_ms": duration_ms,
            "frame_ms": FRAME_MS,
            "energy_u8": energy_b64,
            "heatmap": heatmap or None,
            "chapters": chapters,
        },
    )
    assets["timeline"] = video_key(video_id, "timeline.json")

    transcript_source = None
    if caption_words:
        words = _shift_words(caption_words, offset_ms, duration_ms)
        storage.write_json(
            video_key(video_id, "transcript.json"),
            transcript_to_json(words, source="captions", language=caption_lang),
        )
        assets["transcript"] = video_key(video_id, "transcript.json")
        transcript_source = "captions"

    platform_meta = {
        k: info.get(k)
        for k in (
            "view_count",
            "like_count",
            "comment_count",
            "upload_date",
            "webpage_url",
            "description",
            "tags",
            "thumbnail",
        )
        if info.get(k) is not None
    }
    platform_meta["has_heatmap"] = bool(heatmap)
    platform_meta["chapters"] = chapters
    if offset_ms:
        platform_meta["section_offset_ms"] = offset_ms

    with session_scope() as s:
        video = s.get(Video, video_id)
        if video is None:
            return
        video.assets = assets
        video.meta = {**video.meta, **platform_meta}
        video.duration_ms, video.width, video.height = duration_ms, meta.width, meta.height
        video.fps, video.size_bytes, video.has_audio = meta.fps, src.stat().st_size, meta.has_audio
        video.progress, video.error = 0.0, None
        if transcript_source:
            video.transcript_source, video.language = transcript_source, caption_lang
        if transcript_source or not meta.has_audio:
            video.status = "analyzing"
            enqueue(
                s,
                "video.analyze",
                {"video_id": str(video_id)},
                dedupe_key=f"analyze:{video_id}",
                workspace_id=video.workspace_id,
            )
        else:
            video.status = "transcribing"
            enqueue(
                s,
                "video.transcribe",
                {"video_id": str(video_id)},
                dedupe_key=f"transcribe:{video_id}",
                workspace_id=video.workspace_id,
            )
    log.info("ingest done", extra={"video_id": str(video_id), "captions": bool(transcript_source)})
