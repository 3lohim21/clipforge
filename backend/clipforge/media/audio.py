"""Audio extraction + loudness analysis in a single decode pass.

ffmpeg decodes the source audio once, then `asplit` fans it out to:
  1. a 16 kHz mono FLAC (what Whisper wants; ~45 MB/hour, lossless), and
  2. raw PCM on stdout, from which we compute RMS loudness per 100 ms frame on the fly.

The loudness curve powers the editor waveform and the "energy" signal in moment scoring
(laughter, shouting and hype show up as loudness spikes).
"""

from __future__ import annotations

import base64
import subprocess
import threading
from collections import deque
from pathlib import Path

import numpy as np

from clipforge.media.ffmpeg import FFMPEG, MediaError, ProgressFn

SAMPLE_RATE = 16_000
FRAME_MS = 100
_FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
_FLOOR_DB = -90.0


def extract_audio_with_energy(
    src: Path, out_flac: Path, *, duration_s: float, on_progress: ProgressFn | None = None
) -> np.ndarray:
    """Write `out_flac` and return loudness in dBFS per 100 ms frame (float32)."""
    cmd = [
        FFMPEG,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-filter_complex",
        "[0:a:0]aformat=sample_fmts=s16:sample_rates=16000:channel_layouts=mono,asplit=2[file][pcm]",
        "-map",
        "[file]",
        "-c:a",
        "flac",
        str(out_flac),
        "-map",
        "[pcm]",
        "-f",
        "s16le",
        "pipe:1",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    tail: deque[bytes] = deque(maxlen=20)
    reader = threading.Thread(target=lambda: tail.extend(proc.stderr or []), daemon=True)
    reader.start()

    frames: list[np.ndarray] = []
    carry = np.empty(0, dtype=np.int16)
    chunk_bytes = _FRAME_SAMPLES * 2 * 50  # 5 s of audio per read
    processed_samples = 0
    assert proc.stdout is not None
    while True:
        buf = proc.stdout.read(chunk_bytes)
        if not buf:
            break
        samples = np.frombuffer(buf[: len(buf) - (len(buf) % 2)], dtype=np.int16)
        if carry.size:
            samples = np.concatenate([carry, samples])
        usable = samples.size - (samples.size % _FRAME_SAMPLES)
        if usable:
            block = samples[:usable].astype(np.float32).reshape(-1, _FRAME_SAMPLES) / 32768.0
            rms = np.sqrt(np.mean(block * block, axis=1))
            frames.append(20.0 * np.log10(np.maximum(rms, 1e-9)))
        carry = samples[usable:]
        processed_samples += usable
        if on_progress and duration_s > 0:
            on_progress(min(1.0, processed_samples / SAMPLE_RATE / duration_s))
    code = proc.wait()
    reader.join(timeout=5)
    if code != 0:
        detail = b"".join(tail).decode("utf-8", "replace")[-800:]
        raise MediaError(f"audio extraction failed: {detail}")
    energy = np.concatenate(frames) if frames else np.empty(0, dtype=np.float32)
    return np.maximum(energy, _FLOOR_DB).astype(np.float32)


def energy_to_u8_b64(energy_db: np.ndarray, floor_db: float = -60.0) -> str:
    """Quantise loudness to one byte per frame (-60..0 dBFS -> 0..255) for the browser.
    A 3-hour video becomes ~144 KB of base64 instead of ~1 MB of JSON floats."""
    scaled = np.clip((energy_db - floor_db) / -floor_db * 255.0, 0, 255).astype(np.uint8)
    return base64.b64encode(scaled.tobytes()).decode("ascii")


def energy_from_u8_b64(data: str, floor_db: float = -60.0) -> np.ndarray:
    raw = np.frombuffer(base64.b64decode(data), dtype=np.uint8).astype(np.float32)
    return raw / 255.0 * -floor_db + floor_db
