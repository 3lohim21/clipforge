"""Local speech-to-text with faster-whisper (CTranslate2). No per-minute API fees.

Performance choices:
* The model is loaded once per worker process and reused for every job (loading costs
  seconds and hundreds of MB).
* CPU: int8 quantisation + greedy decoding (beam_size=1) — roughly 2-4x faster than the
  defaults with a small accuracy cost; `small` handles podcasts well.
* GPU: float16 + BatchedInferencePipeline, which transcribes many VAD chunks in parallel
  (an hour of audio in ~1-2 minutes with large-v3-turbo on a modern GPU).
* VAD skips silence entirely, which also suppresses Whisper's hallucinations on quiet parts.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from clipforge.config import Settings
from clipforge.transcription.words import Word, finalize_words

log = logging.getLogger(__name__)

_lock = threading.Lock()
_cache: dict[str, Any] = {}


class TranscriptionInterrupted(RuntimeError):
    pass


def _resolve_device(settings: Settings) -> tuple[str, str]:
    device = settings.whisper_device
    if device == "auto":
        try:
            import ctranslate2

            device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:
            device = "cpu"
    compute = settings.whisper_compute_type
    if compute == "auto":
        compute = "float16" if device == "cuda" else "int8"
    return device, compute


def get_model(settings: Settings) -> tuple[Any, str]:
    """Return (model, device), loading it on first use."""
    with _lock:
        if "model" not in _cache:
            from faster_whisper import WhisperModel

            device, compute = _resolve_device(settings)
            settings.models_cache_dir.mkdir(parents=True, exist_ok=True)
            log.info(
                "loading whisper model",
                extra={"model": settings.whisper_model, "device": device, "compute": compute},
            )
            _cache["model"] = WhisperModel(
                settings.whisper_model,
                device=device,
                compute_type=compute,
                cpu_threads=settings.whisper_cpu_threads,
                num_workers=max(1, settings.worker_concurrency),
                download_root=str(settings.models_cache_dir),
            )
            _cache["device"] = device
        return _cache["model"], _cache["device"]


def transcribe_file(
    audio_path: Path,
    *,
    settings: Settings,
    duration_s: float,
    language: str | None = None,
    on_progress: Callable[[float], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    model: Any = None,
) -> tuple[list[Word], str | None]:
    """Transcribe with word timestamps. Returns (words, detected_language).

    `model` may be injected (tests); otherwise the process-wide model is used.
    """
    device = "cpu"
    if model is None:
        model, device = get_model(settings)

    common: dict[str, Any] = {"language": language, "word_timestamps": True}
    if device == "cuda":
        from faster_whisper import BatchedInferencePipeline

        segments, info = BatchedInferencePipeline(model=model).transcribe(
            str(audio_path), batch_size=16, **common
        )
    else:
        segments, info = model.transcribe(
            str(audio_path),
            beam_size=settings.whisper_beam_size,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            condition_on_previous_text=False,  # prevents runaway repetition on long audio
            **common,
        )

    words: list[Word] = []
    for segment in segments:  # lazy generator: decoding happens as we iterate
        if should_stop and should_stop():
            raise TranscriptionInterrupted("worker shutting down")
        for w in segment.words or []:
            text = (w.word or "").strip()
            if text:
                words.append(
                    Word(int(w.start * 1000), int(w.end * 1000), text, round(float(w.probability), 3))
                )
        if on_progress and duration_s > 0:
            on_progress(min(1.0, float(segment.end) / duration_s))
    return finalize_words(words), getattr(info, "language", None)
