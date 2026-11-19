"""Reframe (face tracking) and the ffmpeg render pipeline on real media."""

from __future__ import annotations

import subprocess
from pathlib import Path

import cv2
import numpy as np
import pytest

from clipforge.config import get_settings
from clipforge.media.ffmpeg import mp4_is_faststart, probe
from clipforge.rendering.compose import crop_x_expression, render_clip
from clipforge.rendering.reframe import compute_track, targets_to_shots
from tests.conftest import FIXTURES, fake_words


def _write_video(path: Path, frames, fps: int = 25, size=(1280, 720)) -> None:  # type: ignore[no-untyped-def]
    proc = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
         "-s", f"{size[0]}x{size[1]}", "-r", str(fps), "-i", "-", "-c:v", "libx264", "-preset", "ultrafast",
         "-pix_fmt", "yuv420p", str(path)],
        stdin=subprocess.PIPE,
    )
    for frame in frames:
        proc.stdin.write(frame.tobytes())  # type: ignore[union-attr]
    proc.stdin.close()  # type: ignore[union-attr]
    assert proc.wait() == 0


@pytest.fixture(scope="module")
def face() -> np.ndarray:
    img = cv2.imread(str(FIXTURES / "face.jpg"))
    assert img is not None
    return img


def _scene(face: np.ndarray, centers: list[tuple[float, bool]], rng: np.random.Generator) -> np.ndarray:
    frame = np.full((720, 1280, 3), 40, np.uint8)
    for cx, talking in centers:
        f = face.copy()
        if talking:  # irregular mouth motion, like speech
            f[88:110, 128:166] = np.clip(f[88:110, 128:166].astype(int) + int(rng.integers(-80, 80)), 0, 255).astype(np.uint8)
        x = int(cx * 1280 - 170)
        frame[160:500, x : x + 340] = f
    return frame


def test_targets_to_shots_hysteresis_ignores_jitter_and_cuts_on_real_moves() -> None:
    jitter = [0.30, 0.32, 0.29, 0.31, 0.30, 0.33] * 4  # small wobble: one shot
    assert len(targets_to_shots(jitter, fps=4, crop_frac=0.32, dur_s=6)) == 1
    move = [0.3] * 8 + [0.7] * 8
    shots = targets_to_shots(move, fps=4, crop_frac=0.32, dur_s=4)
    assert [round(t, 2) for t, _ in shots] == [0.0, 2.0] and shots[1][1] == pytest.approx(0.7, abs=0.01)
    blip = [0.3] * 8 + [0.7] * 2 + [0.3] * 8  # 0.5 s glance away: no cut
    assert len(targets_to_shots(blip, fps=4, crop_frac=0.32, dur_s=4.5)) == 1
    assert targets_to_shots([None, None], fps=4, crop_frac=0.32, dur_s=0.5) == [(0.0, 0.5)]


def test_crop_expression_is_piecewise_and_clamped() -> None:
    expr = crop_x_expression([(0.0, 0.5), (2.0, 0.99), (4.0, 0.0)], src_w=1280, crop_w=405)
    assert expr == "if(lt(t,2.000),438,if(lt(t,4.000),875,0))"


def test_face_tracking_follows_the_subject(tmp_path: Path, face: np.ndarray) -> None:
    rng = np.random.default_rng(1)
    frames = [_scene(face, [(0.25 if i < 100 else 0.75, False)], rng) for i in range(200)]  # 8 s
    src = tmp_path / "moving.mp4"
    _write_video(src, frames)
    track = compute_track(src, 0, 8, 1280, 720, target_aspect=9 / 16, fps=4)
    assert len(track) == 2
    assert track[0][1] == pytest.approx(0.23, abs=0.05)
    assert track[1][0] == pytest.approx(4.0, abs=0.5) and track[1][1] == pytest.approx(0.73, abs=0.05)


def test_active_speaker_is_chosen_in_a_two_shot(tmp_path: Path, face: np.ndarray) -> None:
    rng = np.random.default_rng(7)
    frames = [_scene(face, [(0.17, i >= 100), (0.83, i < 100)], rng) for i in range(200)]
    src = tmp_path / "twoshot.mp4"
    _write_video(src, frames)
    track = compute_track(src, 0, 8, 1280, 720, target_aspect=9 / 16, fps=4)
    assert track[0][1] > 0.6, "right person talks first"
    assert track[-1][1] < 0.4 and 3.5 <= track[-1][0] <= 5.0, "then the crop cuts to the left person"


@pytest.mark.parametrize("layout", ["vertical_fill", "vertical_fit", "square_fill", "landscape"])
def test_render_every_layout(tmp_path: Path, sample_video: Path, layout: str) -> None:
    meta = probe(sample_video)
    spec = {"start_ms": 2_000, "end_ms": 7_000, "layout": layout, "captions": True, "caption_preset": "beast",
            "title_overlay": True, "title": "Test title", "watermark": "@agency", "reframe": "auto"}
    out, thumb = tmp_path / "out.mp4", tmp_path / "out.jpg"
    result = render_clip(spec, src=sample_video, probe=meta, words=fake_words(40), out_path=out,
                         thumb_path=thumb, workdir=tmp_path / "work", settings=get_settings())
    rendered = probe(out)
    assert (rendered.width, rendered.height) == (result.width, result.height)
    assert rendered.vcodec == "h264" and rendered.acodec == "aac" and rendered.pix_fmt == "yuv420p"
    assert abs(rendered.duration_s - 5.0) < 0.25
    assert mp4_is_faststart(out), "exports must stream instantly on social platforms"
    assert thumb.stat().st_size > 1000
