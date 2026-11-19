"""Viral-moment detection: find the best self-contained short clips in a long video.

Pipeline:
  1. split the transcript into sentences (clips only cut on sentence boundaries);
  2. enumerate every window of consecutive sentences whose length is in [min, max];
  3. score each window on independent signals (all normalised to 0..1):
       hook      - does it open strong? question / bold claim / "you" / numbers,
                   and does it avoid starting mid-thought ("and...", "he said...")?
       ending    - does it land on a complete thought (punctuation, pause, no dangling "and")?
       energy    - loudness above the video's own baseline + share of loud peaks
                   (laughter, shouting and hype read as loudness spikes)
       pace      - speech rate in the engaging band; dead air is penalised
       emotion   - density of emotional, story and laughter language
       clarity   - few filler words
       duration  - closeness to the target length
       heat      - YouTube "Most replayed" audience signal, when the platform has it
  4. weighted sum -> 0..100 score, then greedy non-maximum suppression so the returned
     clips don't overlap.

Range aggregates use prefix sums, so each window costs O(1) and a 3-hour podcast
(~40k windows) is analysed in about a second of pure Python.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from itertools import accumulate

import numpy as np

from clipforge.analysis.lexicon import (
    BAD_ENDERS,
    BAD_STARTERS,
    DANGLING_PRONOUNS,
    EMOTION_WORDS,
    FILLERS,
    HOOK_PHRASES,
    LAUGHTER_TOKENS,
    NUMBER_WORDS,
    SOFT_STARTERS,
    STORY_MARKERS,
)
from clipforge.analysis.segmenter import Sentence, normalize_token, split_sentences
from clipforge.transcription.words import Word

FRAME_MS = 100

WEIGHTS: dict[str, float] = {
    "hook": 0.20,
    "ending": 0.12,
    "energy": 0.14,
    "pace": 0.08,
    "emotion": 0.12,
    "clarity": 0.04,
    "duration": 0.08,
    "heat": 0.22,
}

LABELS: dict[str, str] = {
    "hook": "Strong hook",
    "ending": "Complete thought",
    "energy": "High energy",
    "pace": "Fast-paced",
    "emotion": "Emotional / story language",
    "clarity": "Clean delivery",
    "duration": "Ideal length",
    "heat": "Most-replayed section",
}


@dataclass(frozen=True)
class MomentParams:
    min_len_ms: int = 15_000
    max_len_ms: int = 60_000
    target_len_ms: int = 35_000
    max_clips: int | None = None  # None = scale with video length
    min_score: float = 0.30


@dataclass
class Moment:
    start_ms: int
    end_ms: int
    score: int
    raw: float
    title: str
    hook: str
    reason: str
    text: str
    features: dict[str, float] = field(default_factory=dict)
    hashtags: list[str] = field(default_factory=list)


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


def display_score(raw: float) -> int:
    # Concave stretch: spreads the useful 0.4-0.8 band across ~50-85 without clipping.
    return round(100 * _clamp(raw) ** 0.7)


def auto_max_clips(duration_ms: int) -> int:
    return int(_clamp(round(duration_ms / 60_000 / 4), 3, 30))


# --- per-sentence signals -------------------------------------------------------------------


def _hook_score(s: Sentence, prev: Sentence | None, tokens: list[str]) -> tuple[float, bool]:
    toks = [t for t in tokens[s.i0 : s.i1 + 1] if t]
    if not toks:
        return 0.0, False
    head = " ".join(toks[:15])
    score = 0.3
    phrase = any(p.replace("'", "") in head.replace("'", "") for p in HOOK_PHRASES)
    if s.question:
        score += 0.3
    if phrase:
        score += 0.25
    if any(t.isdigit() or t in NUMBER_WORDS or t.startswith("$") for t in toks[:12]):
        score += 0.1
    if any(t in ("you", "your", "youre") for t in toks[:8]):
        score += 0.1
    if any(t in EMOTION_WORDS for t in toks):
        score += 0.1
    first = toks[0]
    if first in BAD_STARTERS:
        score -= 0.45
    elif first in SOFT_STARTERS:
        score -= 0.1
    if first in DANGLING_PRONOUNS:
        score -= 0.25
    if len(toks) < 4 and not s.question:
        score -= 0.15
    if prev is not None and prev.gap_after < 250:
        score -= 0.1  # speaker is mid-flow: likely depends on what came before
    return _clamp(score), phrase


def _ending_score(s: Sentence, tokens: list[str]) -> float:
    last = next((t for t in reversed(tokens[s.i0 : s.i1 + 1]) if t), "")
    score = 0.35
    if s.terminal:
        score += 0.35
    if s.gap_after >= 600:
        score += 0.15
    if s.gap_after >= 1200:
        score += 0.15
    if last in BAD_ENDERS:
        score -= 0.45
    return _clamp(score)


def _make_title(text: str, max_len: int = 70) -> str:
    words = [w for w in text.split() if normalize_token(w) not in FILLERS]
    while words and normalize_token(words[0]) in (BAD_STARTERS | SOFT_STARTERS):
        words = words[1:]
    title = " ".join(words).strip(" ,;:-")
    if len(title) > max_len:
        title = title[:max_len].rsplit(" ", 1)[0].rstrip(",;:-") + "…"
    title = title.rstrip(".")
    return title[:1].upper() + title[1:] if title else ""


def _prefix(values: list[float] | np.ndarray) -> list[float]:
    return [0.0, *accumulate(float(v) for v in values)]


class _Signals:
    """Prefix-summed per-word, per-frame and per-second signals for O(1) window queries."""

    def __init__(
        self,
        words: list[Word],
        tokens: list[str],
        energy_db: np.ndarray | None,
        heatmap: list[list[float]] | None,
        duration_ms: int,
    ) -> None:
        story_starts = [0.0] * len(words)
        for i in range(len(tokens)):
            window = " ".join(t for t in tokens[i : i + 4] if t)
            if any(window.startswith(m.replace("'", "")) for m in STORY_MARKERS):
                story_starts[i] = 1.0
        laugh = [
            1.0 if (w.text.lower() in LAUGHTER_TOKENS or t in ("haha", "hahaha", "lol", "lmao")) else 0.0
            for w, t in zip(words, tokens, strict=True)
        ]
        emo = [1.0 if t in EMOTION_WORDS else 0.0 for t in tokens]
        filler = [1.0 if t in FILLERS else 0.0 for t in tokens]
        dead = [
            max(0.0, (words[i + 1].start - words[i].end) - 800.0) if i + 1 < len(words) else 0.0
            for i in range(len(words))
        ]
        self.emotion = _prefix(
            [e + 2 * lg + 2 * st for e, lg, st in zip(emo, laugh, story_starts, strict=True)]
        )
        self.filler = _prefix(filler)
        self.dead = _prefix(dead)

        self.has_energy = energy_db is not None and energy_db.size > 10
        if self.has_energy:
            assert energy_db is not None
            speech = energy_db[energy_db > -55.0]
            base = speech if speech.size > 10 else energy_db
            self.e_mu = float(base.mean())
            self.e_sd = float(base.std()) or 1.0
            p90 = float(np.percentile(base, 90))
            # Per-frame excitement in 0..1 (0.5 = the video's typical loudness). Averaging a
            # bounded per-frame value (rather than z-scoring the window mean) keeps a window
            # that only partly covers a loud moment ranked below one centred on it.
            excite = (np.clip((energy_db - self.e_mu) / (2.0 * self.e_sd), -1.0, 1.0) + 1.0) / 2.0
            self.excite = _prefix(excite)
            self.peaks = _prefix((energy_db >= p90).astype(np.float32))
            self.n_frames = energy_db.size

        self.has_heat = bool(heatmap) and len(heatmap or []) >= 5
        if self.has_heat:
            assert heatmap is not None
            seconds = max(1, math.ceil(duration_ms / 1000))
            per_sec = np.zeros(seconds, dtype=np.float32)
            values = np.array([p[2] for p in heatmap], dtype=np.float32)
            lo, hi = float(values.min()), float(values.max())
            span = (hi - lo) or 1.0
            for start, end, value in heatmap:
                a, b = int(start // 1000), max(int(start // 1000) + 1, math.ceil(end / 1000))
                per_sec[a : min(b, seconds)] = (value - lo) / span
            self.heat_sec = per_sec
            self.heat = _prefix(per_sec)

    @staticmethod
    def _mean(prefix: list[float], a: int, b: int) -> float:
        return (prefix[b] - prefix[a]) / (b - a) if b > a else 0.0

    def energy_score(self, start_ms: int, end_ms: int) -> float:
        if not self.has_energy:
            return 0.5
        a = min(start_ms // FRAME_MS, self.n_frames - 1)
        b = min(max(a + 1, end_ms // FRAME_MS), self.n_frames)
        level = self._mean(self.excite, a, b)
        peak = _clamp(self._mean(self.peaks, a, b) / 0.5)
        return 0.65 * level + 0.35 * peak

    def heat_score(self, start_ms: int, end_ms: int) -> float:
        a = min(start_ms // 1000, self.heat_sec.size - 1)
        b = min(max(a + 1, math.ceil(end_ms / 1000)), self.heat_sec.size)
        return 0.5 * self._mean(self.heat, a, b) + 0.5 * float(self.heat_sec[a:b].max())


def _weights(has_energy: bool, has_heat: bool) -> dict[str, float]:
    w = dict(WEIGHTS)
    if not has_heat:
        w.pop("heat")
    if not has_energy:
        w["energy"] *= 0.3  # constant 0.5 placeholder: keep it from diluting real signals
    total = sum(w.values())
    return {k: v / total for k, v in w.items()}


def _pace_score(n_words: int, dur_ms: int, dead_ms: float) -> float:
    wps = n_words / max(dur_ms / 1000, 0.001)
    if wps < 1.0:
        pace = 0.0
    elif wps < 2.4:
        pace = (wps - 1.0) / 1.4
    elif wps <= 3.8:
        pace = 1.0
    elif wps <= 5.5:
        pace = 1.0 - (wps - 3.8) / 1.7 * 0.5
    else:
        pace = 0.5
    return pace * (1.0 - _clamp(dead_ms / dur_ms * 4))


def _reason(features: dict[str, float], weights: dict[str, float], question: bool) -> str:
    ranked = sorted(
        (k for k in weights if features.get(k, 0) >= 0.6),
        key=lambda k: features[k] * weights[k],
        reverse=True,
    )
    labels = []
    for k in ranked[:3]:
        labels.append("Opens with a question" if k == "hook" and question else LABELS[k])
    return " · ".join(labels) or "Balanced segment"


def _nms(
    scored: list[tuple[float, int, int, object]], max_clips: int, min_score: float, overlap: float = 0.15
) -> list[tuple[float, int, int, object]]:
    picked: list[tuple[float, int, int, object]] = []
    for cand in sorted(scored, key=lambda c: c[0], reverse=True):
        if len(picked) >= max_clips:
            break
        if cand[0] < min_score and len(picked) >= 3:
            break
        _, a0, a1, _ = cand
        ok = True
        for _, b0, b1, _ in picked:
            inter = min(a1, b1) - max(a0, b0)
            if inter > overlap * min(a1 - a0, b1 - b0):
                ok = False
                break
        if ok:
            picked.append(cand)
    return picked


def find_moments(
    words: list[Word],
    *,
    duration_ms: int,
    energy_db: np.ndarray | None = None,
    heatmap: list[list[float]] | None = None,
    params: MomentParams | None = None,
) -> list[Moment]:
    params = params or MomentParams()
    max_clips = params.max_clips or auto_max_clips(duration_ms)
    sentences = split_sentences(words)
    if len(words) < 30 or len(sentences) < 3:
        return _audio_only_moments(duration_ms, energy_db, heatmap, params, max_clips)

    tokens = [normalize_token(w.text) for w in words]
    sig = _Signals(words, tokens, energy_db, heatmap, duration_ms)
    weights = _weights(sig.has_energy, sig.has_heat)
    hooks = [_hook_score(s, sentences[i - 1] if i else None, tokens) for i, s in enumerate(sentences)]
    endings = [_ending_score(s, tokens) for s in sentences]
    sigma = 0.45 * params.target_len_ms

    scored: list[tuple[float, int, int, object]] = []
    for si, first in enumerate(sentences):
        hook, _ = hooks[si]
        for ei in range(si, len(sentences)):
            last = sentences[ei]
            dur = last.end - first.start
            if dur > params.max_len_ms:
                break
            if dur < params.min_len_ms:
                continue
            a, b = first.i0, last.i1 + 1
            n = b - a
            feats = {
                "hook": hook,
                "ending": endings[ei],
                "energy": sig.energy_score(first.start, last.end),
                "pace": _pace_score(n, dur, sig.dead[b - 1] - sig.dead[a]),
                "emotion": _clamp(((sig.emotion[b] - sig.emotion[a]) / n) / 0.06),
                "clarity": 1.0 - _clamp(((sig.filler[b] - sig.filler[a]) / n) / 0.06),
                "duration": math.exp(-0.5 * ((dur - params.target_len_ms) / sigma) ** 2),
            }
            if sig.has_heat:
                feats["heat"] = sig.heat_score(first.start, last.end)
            raw = sum(weights[k] * feats[k] for k in weights)
            scored.append((raw, first.start, last.end, (si, ei, feats)))

    if not scored:  # every sentence group is outside [min, max]; relax to the audio fallback
        return _audio_only_moments(duration_ms, energy_db, heatmap, params, max_clips)

    moments: list[Moment] = []
    for raw, _, _, payload in _nms(scored, max_clips, params.min_score):
        si, ei, feats = payload  # type: ignore[misc]
        first, last = sentences[si], sentences[ei]
        prev_end = words[first.i0 - 1].end if first.i0 > 0 else 0
        next_start = words[last.i1 + 1].start if last.i1 + 1 < len(words) else duration_ms
        start_ms = max(0, prev_end + 30 if first.i0 > 0 else 0, first.start - 150)
        end_ms = min(duration_ms, max(last.end, next_start - 30), last.end + 350)
        hook_text = (
            first.text
            if len(first.text.split()) >= 4 or si == ei
            else f"{first.text} {sentences[si + 1].text}"
        )
        moments.append(
            Moment(
                start_ms=start_ms,
                end_ms=max(end_ms, start_ms + 1000),
                score=display_score(raw),
                raw=round(raw, 4),
                title=_make_title(hook_text),
                hook=first.text,
                reason=_reason(feats, weights, first.question),
                text=" ".join(w.text for w in words[first.i0 : last.i1 + 1]),
                features={k: round(v, 3) for k, v in feats.items()},
            )
        )
    return moments


def _audio_only_moments(
    duration_ms: int,
    energy_db: np.ndarray | None,
    heatmap: list[list[float]] | None,
    params: MomentParams,
    max_clips: int,
) -> list[Moment]:
    """Music, gameplay or silent footage: rank fixed-length windows by loudness + heatmap."""
    sig = _Signals([], [], energy_db, heatmap, duration_ms)
    length = min(params.target_len_ms, duration_ms)
    if length <= 0:
        return []
    weights = {"energy": 0.4, "heat": 0.6} if sig.has_heat else {"energy": 1.0}
    scored: list[tuple[float, int, int, object]] = []
    for start in range(0, max(1, duration_ms - length + 1), 5000):
        end = start + length
        feats = {"energy": sig.energy_score(start, end)}
        if sig.has_heat:
            feats["heat"] = sig.heat_score(start, end)
        raw = sum(weights[k] * feats[k] for k in weights)
        scored.append((raw, start, end, feats))
    moments = []
    for raw, start, end, feats in _nms(scored, max_clips, 0.0):
        mm, ss = divmod(start // 1000, 60)
        moments.append(
            Moment(
                start_ms=start,
                end_ms=end,
                score=display_score(raw),
                raw=round(raw, 4),
                title=f"Highlight at {mm}:{ss:02d}",
                hook="",
                reason=_reason(feats, weights, False),  # type: ignore[arg-type]
                text="",
                features={k: round(v, 3) for k, v in feats.items()},  # type: ignore[union-attr]
            )
        )
    return moments
