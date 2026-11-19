"""The word-level transcript: the shared data model of analysis, captions and the editor.

Stored compactly as JSON arrays — ``[start_ms, end_ms, text]`` per word — because a
3-hour podcast has ~30k words and this file is fetched by the browser.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

TRANSCRIPT_VERSION = 1


@dataclass(slots=True)
class Word:
    start: int  # milliseconds from the start of the video
    end: int
    text: str
    prob: float | None = None


def _estimated_ms(text: str) -> int:
    return max(150, min(1200, 70 * len(text)))


def finalize_words(words: list[Word]) -> list[Word]:
    """Sort, drop empties, and repair missing/overlapping end times."""
    cleaned = sorted((w for w in words if w.text and w.text.strip()), key=lambda w: w.start)
    for i, w in enumerate(cleaned):
        w.text = w.text.strip()
        end = w.end if w.end > w.start else w.start + _estimated_ms(w.text)
        if i + 1 < len(cleaned):
            nxt = cleaned[i + 1].start
            if nxt > w.start:
                end = min(end, nxt)
        w.end = max(end, w.start + 40)
    return cleaned


def transcript_to_json(words: list[Word], *, source: str, language: str | None) -> dict[str, Any]:
    return {
        "version": TRANSCRIPT_VERSION,
        "source": source,
        "language": language,
        "words": [[w.start, w.end, w.text] for w in words],
    }


def transcript_from_json(data: dict[str, Any]) -> list[Word]:
    return [Word(int(item[0]), int(item[1]), str(item[2])) for item in data.get("words", [])]


def words_in_range(words: list[Word], start_ms: int, end_ms: int) -> list[Word]:
    """Words overlapping [start, end) — binary search, since this runs per render/preview."""
    lo, hi = 0, len(words)
    while lo < hi:
        mid = (lo + hi) // 2
        if words[mid].end <= start_ms:
            lo = mid + 1
        else:
            hi = mid
    out = []
    for w in words[lo:]:
        if w.start >= end_ms:
            break
        out.append(w)
    return out
