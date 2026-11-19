"""Sentence segmentation over word-level transcripts.

Whisper output is punctuated; YouTube auto-captions are not. We detect which we have and
split on terminal punctuation (punctuated) or on speech pauses + a length cap (raw ASR).
Clips are only ever cut on these boundaries, so they start and end on complete thoughts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from clipforge.analysis.lexicon import ABBREVIATIONS
from clipforge.transcription.words import Word

_TERMINAL = re.compile(r"[.?!…]+[\"')\]]*$")
_TOKEN = re.compile(r"[a-z0-9$%']+")


@dataclass(slots=True)
class Sentence:
    i0: int  # index of first word (inclusive)
    i1: int  # index of last word (inclusive)
    start: int  # ms
    end: int  # ms
    text: str
    terminal: bool  # ended with . ? ! (a complete grammatical sentence)
    question: bool
    gap_after: int  # ms of silence before the next sentence


def normalize_token(text: str) -> str:
    return "".join(_TOKEN.findall(text.lower()))


def is_punctuated(words: list[Word]) -> bool:
    if not words:
        return False
    terminal = sum(1 for w in words if _TERMINAL.search(w.text))
    return terminal / len(words) >= 0.02


def split_sentences(words: list[Word]) -> list[Sentence]:
    if not words:
        return []
    punctuated = is_punctuated(words)
    pause_ms = 1500 if punctuated else 650
    max_words = 45 if punctuated else 28

    sentences: list[Sentence] = []
    start_idx = 0
    for i, w in enumerate(words):
        last = i == len(words) - 1
        gap = (words[i + 1].start - w.end) if not last else 10_000
        ends = bool(_TERMINAL.search(w.text)) and w.text.lower() not in ABBREVIATIONS
        if last or (punctuated and ends) or gap >= pause_ms or (i - start_idx + 1) >= max_words:
            chunk = words[start_idx : i + 1]
            sentences.append(
                Sentence(
                    i0=start_idx,
                    i1=i,
                    start=chunk[0].start,
                    end=chunk[-1].end,
                    text=" ".join(x.text for x in chunk),
                    terminal=ends,
                    question=w.text.rstrip("\"')]").endswith("?"),
                    gap_after=max(0, gap),
                )
            )
            start_idx = i + 1
    return sentences
