"""Turn platform subtitle files into word-level transcripts.

YouTube auto-captions (json3) carry a timestamp per word, so for most YouTube imports we
get a usable transcript in milliseconds instead of minutes of speech recognition.
Line-level subtitles (manual captions, WebVTT) get word timings estimated by
distributing each cue's duration across its words in proportion to their length.
"""

from __future__ import annotations

import html
import re
from typing import Any

from clipforge.transcription.words import Word, finalize_words

_TAG = re.compile(r"<[^>]+>")
_VTT_TIME = re.compile(
    r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{3})\s*-->\s*(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{3})"
)
_VTT_INLINE = re.compile(r"<((?:\d+:)?\d{1,2}:\d{2}[.,]\d{3})>")


def _spread(text: str, start_ms: int, end_ms: int) -> list[Word]:
    tokens = text.split()
    if not tokens:
        return []
    span = max(end_ms - start_ms, 80 * len(tokens))
    weights = [len(t) + 2 for t in tokens]  # +2 approximates inter-word pause
    total = sum(weights)
    out: list[Word] = []
    cursor = float(start_ms)
    for token, w in zip(tokens, weights, strict=True):
        dur = span * w / total
        out.append(Word(int(cursor), int(cursor + dur), token))
        cursor += dur
    return out


def parse_json3(data: dict[str, Any]) -> tuple[list[Word], bool]:
    """Returns (words, has_word_timing)."""
    words: list[Word] = []
    word_timed = False
    for event in data.get("events", []):
        segs = event.get("segs")
        if not segs:
            continue
        t0 = int(event.get("tStartMs", 0))
        dur = int(event.get("dDurationMs", 0))
        if len(segs) > 1 or any("tOffsetMs" in s for s in segs):
            word_timed = True
            for seg in segs:
                text = html.unescape(seg.get("utf8", "")).strip()
                if text:
                    start = t0 + int(seg.get("tOffsetMs", 0))
                    words.append(Word(start, start, text))  # end fixed in finalize_words
        else:
            text = html.unescape(" ".join(s.get("utf8", "") for s in segs)).replace("\n", " ")
            words.extend(_spread(text, t0, t0 + dur))
    return finalize_words(words), word_timed


def _ts(h: str | None, m: str, s: str, ms: str) -> int:
    return ((int(h or 0) * 60 + int(m)) * 60 + int(s)) * 1000 + int(ms)


def parse_vtt(text: str) -> tuple[list[Word], bool]:
    words: list[Word] = []
    word_timed = False
    last_text = ""
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n"))
    for block in blocks:
        lines = [ln for ln in block.strip().split("\n") if ln.strip()]
        timing_idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if timing_idx is None:
            continue
        match = _VTT_TIME.search(lines[timing_idx])
        if not match:
            continue
        g = match.groups()
        start, end = _ts(g[0], g[1], g[2], g[3]), _ts(g[4], g[5], g[6], g[7])
        body = " ".join(lines[timing_idx + 1 :])
        if _VTT_INLINE.search(body):
            # Karaoke-style cue: "<00:00:01.200><c> word</c>" gives per-word timing.
            word_timed = True
            pieces = _VTT_INLINE.split(body)
            cursor = start
            for i, piece in enumerate(pieces):
                if i % 2 == 1:
                    m = re.match(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{3})", piece)
                    if m:
                        cursor = _ts(*m.groups())
                    continue
                for token in html.unescape(_TAG.sub("", piece)).split():
                    words.append(Word(cursor, cursor, token))
            continue
        clean = html.unescape(_TAG.sub("", body)).strip()
        # Rolling captions repeat the previous line; only add the new tail.
        if last_text and clean.startswith(last_text):
            clean = clean[len(last_text) :].strip()
        if clean:
            words.extend(_spread(clean, start, end))
            last_text = html.unescape(_TAG.sub("", body)).strip()
    return finalize_words(words), word_timed
