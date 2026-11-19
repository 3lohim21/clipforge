"""Auto-reframe: decide where a 9:16 (or 1:1) crop window should sit over a wider video.

1. Decode only the clip's range, downscaled, at a few fps (ffmpeg seeks straight there).
2. Detect faces (YuNet CNN; Haar cascade fallback), associate them frame to frame.
3. Estimate who is talking from mouth-region motion relative to head motion — a cheap
   active-speaker detector that handles two-shots in podcasts.
4. Convert per-frame targets into stable "shots" with hysteresis: the crop holds still
   and cuts to the new subject only when the change persists, like a human editor,
   instead of drifting with every head movement.

Output is a list of (t_seconds, center_x) keyframes (piecewise-constant), cheap to store
on the clip and turned into an ffmpeg crop expression at render time.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from clipforge.config import MODELS_DIR
from clipforge.media.ffmpeg import FFMPEG

log = logging.getLogger(__name__)

YUNET_MODEL = MODELS_DIR / "face_detection_yunet_2023mar.onnx"
ANALYSIS_WIDTH = 640  # wide two-shots put faces at ~3% of frame width; 480px loses mouths
MIN_SHOT_S = 1.0
MOVE_THRESHOLD = 0.12  # fraction of frame width a subject must move to trigger a new shot
ACTIVITY_FLOOR = 2.0  # mean grey-level change below this is sensor noise, not speech


@dataclass(slots=True)
class Face:
    cx: float  # normalised 0..1
    cy: float
    w: float  # normalised width
    h: float
    box: tuple[int, int, int, int]  # pixel x, y, w, h in the analysis frame
    activity: float = 0.0


class _Detector:
    def __init__(self, size: tuple[int, int]) -> None:
        self.yunet = None
        self.haar = None
        if YUNET_MODEL.exists() and hasattr(cv2, "FaceDetectorYN"):
            try:
                self.yunet = cv2.FaceDetectorYN.create(str(YUNET_MODEL), "", size, 0.6, 0.3, 50)
            except cv2.error:
                log.warning("YuNet unavailable; falling back to Haar cascade")
        if self.yunet is None:
            # OpenCV 5 moved Haar cascades out of the main package; only use them if present.
            if not hasattr(cv2, "CascadeClassifier"):
                raise RuntimeError("no face detector available (YuNet model missing, no Haar cascades)")
            self.haar = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")

    def detect(self, frame: np.ndarray) -> list[tuple[int, int, int, int]]:
        if self.yunet is not None:
            _, faces = self.yunet.detect(frame)
            if faces is None:
                return []
            return [tuple(int(v) for v in f[:4]) for f in faces]  # type: ignore[misc]
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        found = self.haar.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(20, 20))  # type: ignore[union-attr]
        return [tuple(int(v) for v in f) for f in found]  # type: ignore[misc]


def _iter_frames(src: Path, start_s: float, dur_s: float, fps: float, width: int, height: int):
    cmd = [
        FFMPEG,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-ss",
        f"{start_s:.3f}",
        "-i",
        str(src),
        "-t",
        f"{dur_s:.3f}",
        "-vf",
        f"fps={fps},scale={width}:{height}",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "bgr24",
        "pipe:1",
    ]
    frame_bytes = width * height * 3
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        assert proc.stdout is not None
        while True:
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            yield np.frombuffer(buf, dtype=np.uint8).reshape(height, width, 3)
    finally:
        proc.stdout.close()  # type: ignore[union-attr]
        proc.kill()
        proc.wait()


def _region(gray: np.ndarray, box: tuple[int, int, int, int], y0: float, y1: float) -> np.ndarray | None:
    x, y, w, h = box
    top, bottom = y + int(h * y0), y + int(h * y1)
    left, right = x + int(w * 0.2), x + int(w * 0.8)
    top, left = max(0, top), max(0, left)
    bottom, right = min(gray.shape[0], bottom), min(gray.shape[1], right)
    if bottom - top < 4 or right - left < 4:
        return None
    return cv2.resize(gray[top:bottom, left:right], (24, 12), interpolation=cv2.INTER_AREA).astype(np.float32)


def _speaking_activity(prev: np.ndarray, cur: np.ndarray, box: tuple[int, int, int, int]) -> float:
    """Mouth motion minus upper-face motion (cancels head movement / camera shake)."""
    pm, cm = _region(prev, box, 0.62, 1.0), _region(cur, box, 0.62, 1.0)
    pu, cu = _region(prev, box, 0.15, 0.45), _region(cur, box, 0.15, 0.45)
    if pm is None or cm is None or pu is None or cu is None:
        return 0.0
    return max(0.0, float(np.mean(np.abs(cm - pm))) - 0.7 * float(np.mean(np.abs(cu - pu))))


def detect_targets(
    src: Path, start_s: float, dur_s: float, src_w: int, src_h: int, crop_frac: float, fps: float = 4.0
) -> list[float | None]:
    """Per sampled frame, the normalised x the crop should centre on (None = no face)."""
    width = ANALYSIS_WIDTH
    height = max(2, round(src_h * width / src_w / 2) * 2)
    detector = _Detector((width, height))
    targets: list[float | None] = []
    prev_gray: np.ndarray | None = None
    prev_faces: list[Face] = []
    last_target: float | None = None

    for frame in _iter_frames(src, start_s, dur_s, fps, width, height):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = []
        for x, y, w, h in detector.detect(frame):
            if w * h < (width * height) * 0.002:  # ignore tiny background faces
                continue
            faces.append(Face((x + w / 2) / width, (y + h / 2) / height, w / width, h / height, (x, y, w, h)))
        if prev_gray is not None:
            for f in faces:
                f.activity = _speaking_activity(prev_gray, gray, f.box)
                # smooth with the matching face from the previous frame
                match = min(prev_faces, key=lambda p: abs(p.cx - f.cx), default=None)
                if match and abs(match.cx - f.cx) < 0.08:
                    f.activity = 0.6 * f.activity + 0.4 * match.activity

        if not faces:
            targets.append(None)
        elif len(faces) > 1 and prev_gray is None:
            # No motion history yet, so "who is talking" is unknowable on the very first
            # frame. Stay undecided; the first real decision is back-filled to t=0.
            targets.append(None)
        elif len(faces) == 1:
            targets.append(faces[0].cx)
        else:
            left = min(f.cx - f.w / 2 for f in faces)
            right = max(f.cx + f.w / 2 for f in faces)
            if right - left <= crop_frac * 0.9:
                targets.append((left + right) / 2)  # everyone fits: frame the group
            else:
                max_area = max(f.w * f.h for f in faces) or 1.0
                max_act = max(ACTIVITY_FLOOR, *(f.activity for f in faces))
                best, best_score = faces[0], -1.0
                for f in faces:
                    s = 0.45 * (f.w * f.h / max_area) + 0.55 * (f.activity / max_act)
                    if last_target is not None and abs(f.cx - last_target) < 0.06:
                        s += 0.15  # stickiness: don't ping-pong between similar scores
                    if s > best_score:
                        best, best_score = f, s
                targets.append(best.cx)
        if targets[-1] is not None:
            last_target = targets[-1]
        prev_gray, prev_faces = gray, faces
    return targets


def targets_to_shots(
    targets: list[float | None], fps: float, crop_frac: float, dur_s: float
) -> list[tuple[float, float]]:
    """Collapse per-frame targets into stable shots: [(start_s, center_x), ...]."""
    if not targets or all(t is None for t in targets):
        return [(0.0, 0.5)]
    filled: list[float] = []
    first_valid = next(t for t in targets if t is not None)
    last = first_valid
    for t in targets:
        last = t if t is not None else last
        filled.append(last)

    min_len = max(1, round(MIN_SHOT_S * fps))
    half = crop_frac / 2
    shots: list[tuple[int, list[float]]] = [(0, [filled[0]])]
    i = 1
    while i < len(filled):
        start, samples = shots[-1]
        center = float(np.median(samples))
        if abs(filled[i] - center) > MOVE_THRESHOLD:
            window = filled[i : i + min_len]
            if len(window) == min_len and all(abs(v - center) > MOVE_THRESHOLD for v in window):
                shots.append((i, list(window)))
                i += min_len
                continue
        samples.append(filled[i])
        i += 1

    # An opening shot shorter than the minimum is a flash cut at t=0: fold it into the next.
    if len(shots) > 1 and shots[1][0] < min_len:
        shots = [(0, shots[1][1]), *shots[2:]]

    out = []
    for start, samples in shots:
        cx = float(np.median(samples))
        cx = min(max(cx, half), 1 - half)  # keep the crop inside the frame
        out.append((round(start / fps, 3), round(cx, 4)))
    # drop shots that don't move the crop meaningfully (merge into previous)
    merged = [out[0]]
    for t, cx in out[1:]:
        if abs(cx - merged[-1][1]) > MOVE_THRESHOLD / 2 and t < dur_s:
            merged.append((t, cx))
    return merged[:60]


def compute_track(
    src: Path,
    start_s: float,
    dur_s: float,
    src_w: int,
    src_h: int,
    *,
    target_aspect: float,
    fps: float = 4.0,
) -> list[tuple[float, float]]:
    """Face-tracked crop keyframes for a clip. `target_aspect` = out_w / out_h."""
    crop_frac = min(1.0, (src_h * target_aspect) / src_w)
    if crop_frac >= 0.999:
        return [(0.0, 0.5)]  # source is already narrower than the target: nothing to track
    try:
        targets = detect_targets(src, start_s, dur_s, src_w, src_h, crop_frac, fps)
    except Exception:
        log.exception("face tracking failed; using centre crop")
        return [(0.0, 0.5)]
    return targets_to_shots(targets, fps, crop_frac, dur_s)
