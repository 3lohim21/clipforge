"""Import media from a link with yt-dlp (YouTube, Twitch, Kick, TikTok, X, Instagram, Vimeo,
Rumble, direct .mp4 links and ~1,800 other sites).

Speed decisions:
* Format preference is H.264 + AAC in MP4 — the browser plays it directly, so the editor
  never waits for a proxy transcode.
* HLS/DASH fragments download in parallel (`concurrent_fragment_downloads`), the single
  biggest win for Twitch/Kick VODs.
* One metadata extraction serves everything: formats, chapters, the YouTube "Most
  replayed" heatmap, and caption tracks (fetched through the same session/cookies).
* Optional time-range import pulls only part of a 10-hour stream.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clipforge.config import Settings
from clipforge.jobs.queue import PermanentJobError
from clipforge.media.captions import parse_json3, parse_vtt
from clipforge.transcription.words import Word

log = logging.getLogger(__name__)

# yt-dlp error text -> message shown to the user. These can't be fixed by retrying.
_PERMANENT_ERRORS: list[tuple[str, str]] = [
    ("private video", "This video is private."),
    ("video unavailable", "This video is unavailable (removed, region-locked, or private)."),
    ("unsupported url", "This link isn't a video page yt-dlp can read."),
    ("sign in to confirm your age", "Age-restricted video. Configure YTDLP_COOKIES_FILE to import it."),
    (
        "not a bot",
        "The platform blocked this server's IP (bot check). Configure YTDLP_COOKIES_FILE "
        "or YTDLP_PROXY (see docs/DEPLOYMENT.md).",
    ),
    ("members-only", "Members-only video. Configure YTDLP_COOKIES_FILE with a member account."),
    ("join this channel", "Members-only video. Configure YTDLP_COOKIES_FILE with a member account."),
    ("http error 404", "Not found (HTTP 404)."),
    ("http error 403", "Access denied by the host (HTTP 403)."),
    ("http error 401", "This video requires login."),
    ("login required", "This video requires login. Configure YTDLP_COOKIES_FILE."),
    ("requested content is not available", "This content is not available."),
    ("no video formats found", "No downloadable video found at this link."),
    ("this live event will begin", "This live stream hasn't started yet."),
    ("premieres in", "This video hasn't premiered yet."),
]


@dataclass
class DownloadResult:
    path: Path
    info: dict[str, Any]
    caption_words: list[Word] | None = None
    caption_word_timed: bool = False
    caption_language: str | None = None
    section_offset_ms: int = 0
    warnings: list[str] = field(default_factory=list)


class _YdlLogger:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        self.errors.append(msg)


def _friendly_error(message: str) -> str | None:
    lowered = message.lower()
    for needle, friendly in _PERMANENT_ERRORS:
        if needle in lowered:
            return friendly
    return None


def format_selector(max_height: int) -> str:
    h = max_height
    # `?` keeps formats whose height/codec the site doesn't report.
    return "/".join(
        [
            f"bv*[height<=?{h}][vcodec^=avc1]+ba[acodec^=mp4a]",
            f"bv*[height<=?{h}][vcodec^=avc1]+ba",
            f"b[height<=?{h}][vcodec^=?avc1]",
            f"bv*[height<=?{h}]+ba",
            f"b[height<=?{h}]",
            "bv*+ba",
            "b",
        ]
    )


def _pick_caption_track(info: dict[str, Any]) -> tuple[str, str, str] | None:
    """Return (url, ext, language) of the best caption track, or None.

    Preference: the platform's speech-recognition track in the ORIGINAL language (it has
    per-word timing), then human subtitles in the video's language. Auto-translated
    tracks are never used — their timing doesn't match the speech.
    """
    auto: dict[str, list[dict[str, Any]]] = info.get("automatic_captions") or {}
    subs: dict[str, list[dict[str, Any]]] = {
        k: v for k, v in (info.get("subtitles") or {}).items() if k != "live_chat"
    }
    lang = info.get("language")
    candidates: list[tuple[list[dict[str, Any]], str]] = []
    orig = next((k for k in auto if k.endswith("-orig")), None)
    if orig:
        candidates.append((auto[orig], orig.removesuffix("-orig")))
    elif lang and lang in auto:
        candidates.append((auto[lang], lang))
    for key in [lang, "en", "en-US", "en-GB", *subs.keys()]:
        if key and key in subs:
            candidates.append((subs[key], key))
            break
    for formats, track_lang in candidates:
        for ext in ("json3", "vtt"):
            fmt = next((f for f in formats if f.get("ext") == ext and f.get("url")), None)
            if fmt:
                return fmt["url"], ext, track_lang
    return None


def _trim_info(info: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id",
        "title",
        "uploader",
        "channel",
        "duration",
        "webpage_url",
        "extractor_key",
        "thumbnail",
        "view_count",
        "like_count",
        "comment_count",
        "upload_date",
        "language",
    )
    out: dict[str, Any] = {k: info[k] for k in keys if info.get(k) is not None}
    out["description"] = (info.get("description") or "")[:2000]
    out["tags"] = list(info.get("tags") or [])[:30]
    out["chapters"] = [
        {
            "start_ms": int(float(c.get("start_time") or 0) * 1000),
            "end_ms": int(float(c.get("end_time") or 0) * 1000),
            "title": str(c.get("title") or ""),
        }
        for c in info.get("chapters") or []
    ]
    heatmap = info.get("heatmap") or []
    out["heatmap"] = [
        [int(float(p["start_time"]) * 1000), int(float(p["end_time"]) * 1000), float(p["value"])]
        for p in heatmap
        if p.get("value") is not None
    ]
    return out


def download(
    url: str,
    dest_dir: Path,
    *,
    settings: Settings,
    on_progress: Callable[[float], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    section: tuple[float, float] | None = None,
    want_captions: bool = True,
) -> DownloadResult:
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadCancelled, DownloadError, download_range_func

    dest_dir.mkdir(parents=True, exist_ok=True)
    ylog = _YdlLogger()
    sizes: dict[str, float] = {}
    done: dict[str, float] = {}

    def hook(d: dict[str, Any]) -> None:
        if should_stop and should_stop():
            raise DownloadCancelled("worker shutting down")
        if d.get("status") != "downloading" or not on_progress:
            return
        fid = str((d.get("info_dict") or {}).get("format_id") or "main")
        done[fid] = float(d.get("downloaded_bytes") or 0)
        if fid not in sizes or not sizes[fid]:
            sizes[fid] = float(d.get("total_bytes") or d.get("total_bytes_estimate") or 0)
        total = sum(sizes.values())
        if total > 0:
            on_progress(min(0.99, sum(done.values()) / total))

    opts: dict[str, Any] = {
        "format": format_selector(settings.download_max_height),
        "merge_output_format": "mp4",
        "outtmpl": str(dest_dir / "source.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": ylog,
        "progress_hooks": [hook],
        "retries": 5,
        "fragment_retries": 10,
        "extractor_retries": 3,
        "concurrent_fragment_downloads": settings.ytdlp_concurrent_fragments,
        "http_chunk_size": 10 * 1024 * 1024,  # sidesteps YouTube's per-connection throttling
        "socket_timeout": 30,
        "overwrites": True,
        "cachedir": str(settings.data_dir / "cache" / "yt-dlp"),
    }
    if settings.ytdlp_cookies_file:
        opts["cookiefile"] = str(settings.ytdlp_cookies_file)
    if settings.ytdlp_proxy:
        opts["proxy"] = settings.ytdlp_proxy
    if section:
        opts["download_ranges"] = download_range_func(None, [section])

    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info is None:
                raise PermanentJobError("No video found at this link.")
            if info.get("_type") in ("playlist", "multi_video"):
                entries = [e for e in info.get("entries") or [] if e]
                if not entries:
                    raise PermanentJobError("This playlist is empty.")
                raise PermanentJobError(
                    f"That's a playlist ({len(entries)} videos). Paste individual video links."
                )
            if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming", "post_live"):
                raise PermanentJobError(
                    "This is a live (or still-processing) stream. Import it once the VOD is available."
                )
            duration = float(info.get("duration") or 0)
            if duration and duration > settings.max_source_duration_s and not section:
                raise PermanentJobError(
                    f"Video is {duration / 3600:.1f} h long; the limit is "
                    f"{settings.max_source_duration_s / 3600:.1f} h. Import a time range instead."
                )
            info = ydl.process_ie_result(info, download=True)
            downloads = info.get("requested_downloads") or []
            path = Path(downloads[0]["filepath"]) if downloads else None
            if path is None or not path.exists():
                candidates = sorted(p for p in dest_dir.glob("source.*") if p.suffix != ".part")
                path = candidates[0] if candidates else None
            if path is None:
                raise RuntimeError("yt-dlp reported success but produced no file")

            result = DownloadResult(path=path, info=_trim_info(info))
            if section:
                result.section_offset_ms = int(section[0] * 1000)
            if want_captions:
                track = _pick_caption_track(info)
                if track:
                    track_url, ext, lang = track
                    try:
                        raw = ydl.urlopen(track_url).read().decode("utf-8", "replace")
                        words, timed = parse_json3(json.loads(raw)) if ext == "json3" else parse_vtt(raw)
                        if len(words) >= 20:
                            result.caption_words, result.caption_word_timed = words, timed
                            result.caption_language = lang.split("-")[0]
                    except Exception as exc:  # captions are an optimisation; ASR is the fallback
                        result.warnings.append(f"captions unavailable: {exc}")
            return result
    except DownloadCancelled:
        raise
    except DownloadError as exc:
        message = " | ".join(ylog.errors) or str(exc)
        friendly = _friendly_error(message)
        if friendly:
            raise PermanentJobError(friendly) from exc
        raise RuntimeError(f"Download failed: {message[-500:]}") from exc
