"""Thin, safe wrappers around ffmpeg/ffprobe.

* argv lists only — never a shell — so file names can't inject commands.
* stderr is drained on a side thread (a full pipe would deadlock the child).
* progress comes from `-progress pipe:1`, so callers get a real 0..1 fraction.
* `should_stop` lets a worker kill a long encode during shutdown.
"""

from __future__ import annotations

import json
import shutil
import struct
import subprocess
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"

ProgressFn = Callable[[float], None]
StopFn = Callable[[], bool]


class MediaError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProbeResult:
    duration_s: float
    format_name: str
    size_bytes: int
    has_video: bool
    has_audio: bool
    width: int | None = None  # display width (rotation applied)
    height: int | None = None
    fps: float | None = None
    vcodec: str | None = None
    pix_fmt: str | None = None
    acodec: str | None = None
    rotation: int = 0

    @property
    def browser_playable(self) -> bool:
        """True when Chrome/Safari/Firefox can play the file as-is (no proxy transcode)."""
        container_ok = any(f in self.format_name for f in ("mp4", "mov"))
        video_ok = self.vcodec == "h264" and self.pix_fmt in ("yuv420p", "yuvj420p")
        audio_ok = (not self.has_audio) or self.acodec in ("aac", "mp3")
        return container_ok and video_ok and audio_ok


def _parse_fps(rate: str | None) -> float | None:
    if not rate or rate in ("0/0", "0"):
        return None
    num, _, den = rate.partition("/")
    try:
        value = float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return None
    return round(value, 3) if 0 < value < 1000 else None


def _rotation(stream: dict) -> int:
    for side in stream.get("side_data_list", []) or []:
        if "rotation" in side:
            try:
                return round(float(side["rotation"])) % 360
            except (TypeError, ValueError):
                return 0
    tag = (stream.get("tags") or {}).get("rotate")
    try:
        return int(tag) % 360 if tag else 0
    except ValueError:
        return 0


def probe(path: Path) -> ProbeResult:
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        ).stdout
    except subprocess.CalledProcessError as exc:
        raise MediaError(f"Unreadable media file: {exc.stderr.strip()[-300:]}") from exc
    data = json.loads(out or "{}")
    fmt = data.get("format", {})
    streams = data.get("streams", [])
    video = next(
        (
            s
            for s in streams
            if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")
        ),
        None,
    )
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = float(fmt.get("duration") or 0) or float((video or audio or {}).get("duration") or 0)
    width = height = None
    rotation = 0
    if video:
        width, height = video.get("width"), video.get("height")
        rotation = _rotation(video)
        if rotation in (90, 270) and width and height:
            width, height = height, width
    return ProbeResult(
        duration_s=duration,
        format_name=fmt.get("format_name", ""),
        size_bytes=int(fmt.get("size") or path.stat().st_size),
        has_video=video is not None,
        has_audio=audio is not None,
        width=width,
        height=height,
        fps=_parse_fps(video.get("avg_frame_rate") or video.get("r_frame_rate")) if video else None,
        vcodec=video.get("codec_name") if video else None,
        pix_fmt=video.get("pix_fmt") if video else None,
        acodec=audio.get("codec_name") if audio else None,
        rotation=rotation,
    )


def mp4_is_faststart(path: Path) -> bool:
    """True when the `moov` index precedes `mdat`, i.e. playback can start before the whole
    file is fetched. Reads only box headers, so it's instant even on multi-GB files."""
    try:
        with path.open("rb") as f:
            while True:
                header = f.read(8)
                if len(header) < 8:
                    return False
                size, kind = struct.unpack(">I4s", header)
                if kind == b"moov":
                    return True
                if kind == b"mdat":
                    return False
                if size == 1:
                    size = struct.unpack(">Q", f.read(8))[0]
                    f.seek(size - 16, 1)
                elif size < 8:
                    return False
                else:
                    f.seek(size - 8, 1)
    except OSError:
        return False


def run_ffmpeg(
    args: list[str],
    *,
    duration_s: float | None = None,
    on_progress: ProgressFn | None = None,
    should_stop: StopFn | None = None,
    timeout_s: float | None = None,
) -> None:
    cmd = [
        FFMPEG,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-loglevel",
        "error",
        "-progress",
        "pipe:1",
        "-nostats",
        *args,
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    tail: deque[str] = deque(maxlen=40)

    def drain() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            tail.append(line.rstrip())

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()

    timer: threading.Timer | None = None
    if timeout_s:
        timer = threading.Timer(timeout_s, proc.kill)
        timer.start()
    stopped = False
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if should_stop and should_stop():
                stopped = True
                proc.kill()
                break
            key, _, value = line.strip().partition("=")
            if key == "out_time_us" and on_progress and duration_s and value.isdigit():
                on_progress(min(1.0, int(value) / 1_000_000 / duration_s))
        code = proc.wait()
    finally:
        if timer:
            timer.cancel()
        reader.join(timeout=5)
    if stopped:
        raise MediaError("ffmpeg interrupted by shutdown")
    if code != 0:
        detail = "\n".join(tail)[-1500:] or f"exit code {code}"
        raise MediaError(f"ffmpeg failed: {detail}")
    if on_progress:
        on_progress(1.0)


def make_poster(src: Path, dest: Path, at_s: float, width: int = 640) -> None:
    run_ffmpeg(
        [
            "-ss",
            f"{max(0.0, at_s):.3f}",
            "-i",
            str(src),
            "-frames:v",
            "1",
            "-vf",
            f"scale={width}:-2",
            "-q:v",
            "4",
            str(dest),
        ]
    )


@lru_cache(maxsize=1)
def available_encoders() -> frozenset[str]:
    try:
        out = subprocess.run(
            [FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=30
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    names = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 6:
            names.add(parts[1])
    return frozenset(names)


@lru_cache(maxsize=1)
def nvenc_usable() -> bool:
    """Encoder compiled in AND a GPU actually present (cheap 1-frame smoke test)."""
    if "h264_nvenc" not in available_encoders():
        return False
    try:
        subprocess.run(
            [
                FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=s=256x256:d=0.1",
                "-c:v",
                "h264_nvenc",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            timeout=30,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True
