"""Render a clip in ONE ffmpeg pass: seek -> reframe/compose -> burn captions ->
normalise loudness -> encode (x264, or NVENC when a GPU is present).

Performance notes:
* Input seeking (`-ss` before `-i`) jumps to the nearest keyframe, so rendering minute 95
  of a 3-hour stream doesn't decode the first 94 minutes.
* The blurred background for "full frame" layouts is blurred at 1/4 resolution, then
  upscaled — ~16x fewer pixels through the expensive blur.
* Face tracking samples only the clip's own range at ~4 fps and reuses a stored track
  when the clip bounds haven't changed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clipforge.config import FONTS_DIR, Settings
from clipforge.media.ffmpeg import ProbeResult, make_poster, nvenc_usable, run_ffmpeg
from clipforge.rendering.captions import build_ass, build_chunks, clip_chunks
from clipforge.rendering.presets import get_layout, get_preset
from clipforge.rendering.reframe import compute_track
from clipforge.transcription.words import Word, words_in_range

log = logging.getLogger(__name__)


@dataclass
class RenderResult:
    width: int
    height: int
    duration_ms: int
    track: list[tuple[float, float]] | None


def _even(n: float) -> int:
    return max(2, int(n) // 2 * 2)


def _quote(value: str) -> str:
    """Quote a value for an ffmpeg filter option (single quotes; ' handled specially)."""
    return "'" + value.replace("'", "'\\''") + "'"


def crop_x_expression(track: list[tuple[float, float]], src_w: int, crop_w: int) -> str:
    """Piecewise-constant x(t) for ffmpeg's crop filter: if(lt(t,T1),X0,if(lt(t,T2),X1,...))."""
    xs = [min(max(round(cx * src_w - crop_w / 2), 0), src_w - crop_w) for _, cx in track]
    expr = str(xs[-1])
    for i in range(len(track) - 2, -1, -1):
        expr = f"if(lt(t,{track[i + 1][0]:.3f}),{xs[i]},{expr})"
    return expr


def _video_chain(
    layout_id: str, probe: ProbeResult, track: list[tuple[float, float]] | None
) -> tuple[str, int, int]:
    """Returns (filter_complex for [0:v] -> [base], out_w, out_h)."""
    layout = get_layout(layout_id)
    W, H = layout.width, layout.height
    sw, sh = probe.width or W, probe.height or H
    head = "[0:v]setpts=PTS-STARTPTS"

    if layout.id in ("vertical_fill", "square_fill"):
        aspect = W / H
        if sw / sh > aspect + 1e-3:  # source wider than target: horizontal crop follows faces
            crop_w = _even(sh * aspect)
            x = crop_x_expression(track or [(0.0, 0.5)], sw, crop_w)
            chain = f"{head},crop=w={crop_w}:h={_even(sh)}:x={_quote(x)}:y=0"
        else:  # source taller than target: centre crop vertically
            crop_h = _even(sw / aspect)
            chain = f"{head},crop=w={_even(sw)}:h={crop_h}:x=0:y=(ih-{crop_h})/2"
        return f"{chain},scale={W}:{H}:flags=lanczos,setsar=1[base]", W, H

    if layout.id == "vertical_fit":
        bw, bh = W // 4, H // 4
        return (
            f"{head},split=2[bgsrc][fgsrc];"
            f"[bgsrc]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
            f"boxblur=10:2,eq=brightness=-0.08:saturation=1.15,scale={W}:{H},setsar=1[bg];"
            f"[fgsrc]scale={W}:-2:flags=lanczos,setsar=1[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2[base]",
            W,
            H,
        )

    # landscape: letterbox/pillarbox into 1920x1080
    return (
        f"{head},scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos,"
        f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[base]",
        W,
        H,
    )


def _encoder_args(settings: Settings) -> list[str]:
    use_nvenc = settings.render_encoder == "h264_nvenc" or (
        settings.render_encoder == "auto" and nvenc_usable()
    )
    if use_nvenc:
        return [
            "-c:v",
            "h264_nvenc",
            "-preset",
            "p5",
            "-tune",
            "hq",
            "-rc",
            "vbr",
            "-cq",
            str(settings.render_crf + 1),
            "-b:v",
            "0",
            "-pix_fmt",
            "yuv420p",
        ]
    return [
        "-c:v",
        "libx264",
        "-preset",
        settings.render_preset,
        "-crf",
        str(settings.render_crf),
        "-profile:v",
        "high",
        "-pix_fmt",
        "yuv420p",
    ]


def render_clip(
    spec: dict[str, Any],
    *,
    src: Path,
    probe: ProbeResult,
    words: list[Word],
    out_path: Path,
    thumb_path: Path,
    workdir: Path,
    settings: Settings,
    on_progress: Callable[[float], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> RenderResult:
    start_ms, end_ms = int(spec["start_ms"]), int(spec["end_ms"])
    dur_s = (end_ms - start_ms) / 1000
    layout = get_layout(spec.get("layout"))
    workdir.mkdir(parents=True, exist_ok=True)

    # 1. reframe track (only for face-following layouts on wider-than-target sources)
    track: list[tuple[float, float]] | None = None
    needs_track = (
        layout.id in ("vertical_fill", "square_fill")
        and probe.width
        and probe.height
        and probe.width / probe.height > layout.width / layout.height + 1e-3
    )
    if needs_track:
        mode = spec.get("reframe", "auto")
        if mode == "manual" and spec.get("crop_x") is not None:
            track = [(0.0, float(spec["crop_x"]))]
        elif mode == "center":
            track = [(0.0, 0.5)]
        elif spec.get("track"):
            track = [(float(t), float(x)) for t, x in spec["track"]]
        else:
            track = compute_track(
                src,
                start_ms / 1000,
                dur_s,
                probe.width,
                probe.height,  # type: ignore[arg-type]
                target_aspect=layout.width / layout.height,
                fps=settings.reframe_sample_fps,
            )
        if on_progress:
            on_progress(0.15)

    # 2. filter graph
    graph, out_w, out_h = _video_chain(layout.id, probe, track)
    preset = get_preset(spec.get("caption_preset"))
    chunks = (
        clip_chunks(build_chunks(words_in_range(words, start_ms, end_ms), preset), start_ms, end_ms)
        if spec.get("captions", True) and words
        else []
    )
    title = spec.get("title") if spec.get("title_overlay") else None
    watermark = spec.get("watermark") or None
    if chunks or title or watermark:
        ass_path = workdir / "captions.ass"
        ass_path.write_text(
            build_ass(
                chunks,
                preset,
                width=out_w,
                height=out_h,
                position=float(spec.get("caption_position") or 0.72),
                highlight=spec.get("highlight_color") or None,
                title=title,
                watermark=watermark,
                duration_ms=end_ms - start_ms,
            ),
            encoding="utf-8",
        )
        graph = graph.replace(
            "[base]",
            f"[base0];[base0]ass=filename={_quote(str(ass_path))}:fontsdir={_quote(str(FONTS_DIR))}[base]",
        )
    graph += ";[base]null[v]"

    args = ["-ss", f"{start_ms / 1000:.3f}", "-t", f"{dur_s:.3f}", "-i", str(src)]
    maps = ["-map", "[v]"]
    if probe.has_audio:
        audio = "loudnorm=I=-14:TP=-1.5:LRA=11," if settings.render_audio_normalize else ""
        graph += f";[0:a:0]asetpts=PTS-STARTPTS,{audio}aresample=48000[a]"
        maps += ["-map", "[a]", "-c:a", "aac", "-b:a", "160k", "-ar", "48000"]
    args += [
        "-filter_complex",
        graph,
        *maps,
        *_encoder_args(settings),
        "-fpsmax",
        "60",
        "-movflags",
        "+faststart",
        str(out_path),
    ]

    base = 0.15 if needs_track else 0.0
    run_ffmpeg(
        args,
        duration_s=dur_s,
        on_progress=(lambda f: on_progress(base + (0.97 - base) * f)) if on_progress else None,
        should_stop=should_stop,
    )
    make_poster(out_path, thumb_path, at_s=min(1.0, dur_s / 2), width=360)
    return RenderResult(out_w, out_h, end_ms - start_ms, track)
