"""Viral-moment engine, transcript parsing, captions and LLM re-ranking."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import numpy as np

from clipforge.analysis.llm import rerank
from clipforge.analysis.scorer import MomentParams, find_moments
from clipforge.analysis.segmenter import split_sentences
from clipforge.config import Settings
from clipforge.media.captions import parse_json3, parse_vtt
from clipforge.rendering.captions import ass_escape, build_ass, build_chunks, clip_chunks
from clipforge.rendering.presets import CAPTION_PRESETS, get_preset
from clipforge.transcription.words import Word, words_in_range
from tests.conftest import fake_words


def _sentence_words(sentences: list[str], start_ms: int = 0, gap_ms: int = 700) -> list[Word]:
    words, t = [], start_ms
    for sentence in sentences:
        for tok in sentence.split():
            words.append(Word(t, t + 250, tok))
            t += 320
        t += gap_ms
    return words


# --- transcript import --------------------------------------------------------------------------

def test_parse_json3_word_timed_auto_captions() -> None:
    data = {
        "events": [
            {"tStartMs": 1000, "dDurationMs": 3000, "segs": [
                {"utf8": "hello"}, {"utf8": " world", "tOffsetMs": 400}, {"utf8": " again", "tOffsetMs": 900}]},
            {"tStartMs": 3000, "aAppend": 1, "segs": [{"utf8": "\n"}]},
            {"tStartMs": 4000, "dDurationMs": 1000, "segs": [{"utf8": "[Music]"}]},
        ]
    }
    words, timed = parse_json3(data)
    assert timed
    assert [(w.start, w.text) for w in words] == [(1000, "hello"), (1400, "world"), (1900, "again"), (4000, "[Music]")]
    assert all(w.end > w.start for w in words)
    assert words[0].end <= words[1].start  # ends repaired to next start


def test_parse_json3_line_level_spreads_words() -> None:
    words, timed = parse_json3({"events": [{"tStartMs": 0, "dDurationMs": 2000, "segs": [{"utf8": "one two three"}]}]})
    assert not timed
    assert [w.text for w in words] == ["one", "two", "three"]
    assert words[0].start == 0 and words[-1].end <= 2000


def test_parse_vtt_rolling_and_karaoke() -> None:
    vtt = (
        "WEBVTT\n\n"
        "00:00:01.000 --> 00:00:02.000\nhello there\n\n"
        "00:00:02.000 --> 00:00:03.000\nhello there friend\n\n"
        "00:00:05.000 --> 00:00:06.000\n<00:00:05.000><c> karaoke</c><00:00:05.500><c> words</c>\n"
    )
    words, timed = parse_vtt(vtt)
    assert [w.text for w in words] == ["hello", "there", "friend", "karaoke", "words"]
    assert timed
    assert words[3].start == 5000 and words[4].start == 5500


def test_words_in_range_binary_search() -> None:
    words = fake_words(120)
    picked = words_in_range(words, 30_000, 40_000)
    assert picked and all(w.end > 30_000 and w.start < 40_000 for w in picked)
    assert picked == [w for w in words if w.end > 30_000 and w.start < 40_000]


# --- moment detection -------------------------------------------------------------------------------

def test_segmenter_splits_punctuated_and_raw_asr() -> None:
    punct = split_sentences(_sentence_words(["This is one.", "Is this two?", "Three!"]))
    assert [s.text for s in punct] == ["This is one.", "Is this two?", "Three!"]
    assert punct[1].question and punct[0].terminal
    raw = _sentence_words(["this is one", "this is two"], gap_ms=900)
    assert len(split_sentences(raw)) == 2  # no punctuation: split on the pause


def test_find_moments_prefers_hooks_and_never_overlaps() -> None:
    filler = "and um so yeah we were just kind of talking about stuff and things you know like whatever."
    hook = (
        "What if I told you the biggest mistake people make with money is waiting? "
        "I remember the day I lost a million dollars in one trade. It was insane. "
        "Here's why you need to invest early. Never risk money you can't afford to lose."
    )
    sentences = [filler] * 12 + hook.split(". ") + [filler] * 12
    words = _sentence_words([s if s.endswith((".", "?", "!")) else s + "." for s in sentences])
    duration = words[-1].end + 1000
    moments = find_moments(words, duration_ms=duration, params=MomentParams(min_len_ms=10_000, max_len_ms=40_000, target_len_ms=20_000, max_clips=5))
    assert moments, "should find something"
    best = moments[0]
    hook_start = next(w.start for w in words if w.text == "What")
    assert best.start_ms <= hook_start + 1000 and best.end_ms > hook_start, "the hook segment should rank first"
    assert best.score >= max(m.score for m in moments)
    for i, a in enumerate(moments):
        assert 10_000 - 1000 <= a.end_ms - a.start_ms <= 40_000 + 1000
        for b in moments[i + 1:]:
            inter = min(a.end_ms, b.end_ms) - max(a.start_ms, b.start_ms)
            assert inter <= 0.15 * min(a.end_ms - a.start_ms, b.end_ms - b.start_ms) + 1
    assert best.title and not best.title.lower().startswith(("and ", "um", "so "))


def test_heatmap_pulls_selection_toward_most_replayed() -> None:
    words = fake_words(600)
    duration = 600_000
    heat = [[i * 6000, (i + 1) * 6000, 1.0 if 50 <= i < 55 else 0.05] for i in range(100)]
    moments = find_moments(words, duration_ms=duration, heatmap=heat, params=MomentParams(max_clips=3))
    assert any(m.start_ms < 330_000 and m.end_ms > 300_000 for m in moments[:2])
    assert "heat" in moments[0].features


def test_audio_only_fallback_uses_loudness() -> None:
    energy = np.full(3000, -40.0, dtype=np.float32)  # 300 s at 100 ms frames
    energy[1500:1800] = -8.0  # loud section 150-180 s
    moments = find_moments([], duration_ms=300_000, energy_db=energy, params=MomentParams(target_len_ms=30_000, max_clips=2))
    assert moments[0].start_ms <= 150_000 and moments[0].end_ms >= 175_000
    assert moments[0].title.startswith("Highlight at")


def test_scoring_three_hour_podcast_is_fast() -> None:
    words = fake_words(3 * 3600)
    energy = np.random.default_rng(1).normal(-25, 6, 3 * 36000).astype(np.float32)
    t0 = time.perf_counter()
    moments = find_moments(words, duration_ms=3 * 3600 * 1000, energy_db=energy)
    elapsed = time.perf_counter() - t0
    assert len(moments) == 30
    assert elapsed < 20, f"scoring took {elapsed:.1f}s"


# --- captions -----------------------------------------------------------------------------------------

def test_chunks_respect_limits_breaks_and_drop_fillers() -> None:
    words = _sentence_words(["Here's why most people never get rich.", "um They spend it all."], gap_ms=200)
    chunks = build_chunks(words, get_preset("bold"))
    texts = [" ".join(w.text for w in c.words) for c in chunks]
    assert texts[0] == "HERE'S WHY MOST"
    assert any(t.endswith("RICH") for t in texts)  # sentence end closes a chunk
    assert all("UM" not in t.split() for t in texts)
    assert all(len(c.words) <= 3 for c in chunks)
    for a, b in zip(chunks, chunks[1:], strict=False):
        assert a.end <= b.start  # never two chunks on screen at once


def test_clip_chunks_retime_relative_to_clip() -> None:
    chunks = build_chunks(fake_words(60), get_preset("clean"))
    clipped = clip_chunks(chunks, 10_000, 20_000)
    assert clipped[0].start >= 0 and clipped[-1].end <= 10_000
    assert all(0 <= w.start for c in clipped for w in c.words)


def test_ass_output_escapes_overrides_and_formats_times() -> None:
    assert ass_escape("{\\b1}evil") == "(⧵b1)evil"
    words = [Word(0, 400, "{\\pos(1,1)}hack"), Word(400, 900, "safe")]
    for preset in CAPTION_PRESETS.values():
        ass = build_ass(build_chunks(words, preset), preset, width=1080, height=1920, title="My title", watermark="@me", duration_ms=5000)
        assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass
        assert "\\pos(1,1)" not in ass.split("[Events]")[1].replace("\\pos(540", "")
        assert "Dialogue:" in ass and "My title" in ass and "@me" in ass
    karaoke = build_ass(build_chunks(words, get_preset("karaoke")), get_preset("karaoke"), width=1080, height=1920)
    assert "\\kf" in karaoke


# --- LLM re-ranking ---------------------------------------------------------------------------------------

class _FakeMessages:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[dict] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return self.response


def _fake_client(payload: object, stop_reason: str = "end_turn") -> SimpleNamespace:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    response = SimpleNamespace(
        stop_reason=stop_reason, model="claude-test",
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=100, output_tokens=50),
    )
    return SimpleNamespace(beta=SimpleNamespace(messages=_FakeMessages(response)))


def test_llm_rerank_merges_scores_titles_and_hashtags() -> None:
    moments = find_moments(fake_words(400), duration_ms=400_000, params=MomentParams(max_clips=6))
    settings = Settings(anthropic_api_key="test", llm_model="claude-opus-5-5")
    client = _fake_client({"clips": [
        {"id": 2, "score": 97, "standalone": True, "title": "The million dollar mistake", "hook": "He lost it ALL",
         "hashtags": ["#money", "investing"], "reason": "Huge stakes"},
        {"id": 0, "score": 20, "standalone": False, "title": "meh", "hook": "", "hashtags": [], "reason": ""},
        {"id": 99, "score": 100, "standalone": True, "title": "bogus id", "hook": "", "hashtags": [], "reason": ""},
    ]})
    ranked, info = rerank(moments, video={"title": "Pod"}, settings=settings, keep=2, client=client)
    assert info["used"] and len(ranked) == 2
    assert ranked[0].title == "The million dollar mistake" and ranked[0].hashtags == ["money", "investing"]
    call = client.beta.messages.calls[0]
    assert call["model"] == "claude-opus-5-5" and call["fallbacks"] == "default"
    assert call["output_config"]["format"]["type"] == "json_schema"


def test_llm_rerank_degrades_gracefully() -> None:
    moments = find_moments(fake_words(300), duration_ms=300_000, params=MomentParams(max_clips=4))
    settings = Settings(anthropic_api_key="test")
    for client in (_fake_client({"clips": []}, stop_reason="refusal"), _fake_client("not json at all")):
        ranked, info = rerank(moments, video={}, settings=settings, keep=3, client=client)
        assert not info["used"] and len(ranked) == 3
    ranked, info = rerank(moments, video={}, settings=Settings(anthropic_api_key=None), keep=2)
    assert not info["used"] and len(ranked) == 2  # no key: heuristic only, no network
