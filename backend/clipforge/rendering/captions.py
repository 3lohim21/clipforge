"""Animated captions: group words into short on-screen chunks, then emit an ASS script that
libass burns into the video.

"highlight" mode emits one event per spoken word — the chunk stays on screen while the
active word changes colour (and optionally pops). "karaoke" sweeps colour across each
word with \\kf. Events are contiguous (each ends where the next begins), so nothing
flickers between words.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from clipforge.analysis.lexicon import FILLERS
from clipforge.analysis.segmenter import normalize_token
from clipforge.rendering.presets import TITLE_OVERLAY, WATERMARK_OVERLAY, CaptionPreset
from clipforge.transcription.words import Word

_NON_SPEECH = re.compile(r"^[\[(].*[\])]$")  # [Music], (laughs), [Applause]
_STRIP_PUNCT = re.compile(r"[.,;:…]+$")
_SENTENCE_END = re.compile(r"[.?!…][\"')\]]*$")


@dataclass(slots=True)
class CaptionChunk:
    start: int
    end: int
    words: list[Word]

    def to_json(self) -> list[object]:
        return [self.start, self.end, [[w.start, w.end, w.text] for w in self.words]]


def _display_text(text: str, preset: CaptionPreset) -> str:
    if preset.strip_punctuation:
        text = _STRIP_PUNCT.sub("", text)
    return text.upper() if preset.uppercase else text


def build_chunks(words: list[Word], preset: CaptionPreset) -> list[CaptionChunk]:
    """Group a transcript (any length) into caption chunks for `preset`. Times are absolute."""
    chunks: list[CaptionChunk] = []
    current: list[Word] = []
    chars = 0
    prev_ended_sentence = False  # judged on the ORIGINAL text; display text may lose its "."
    for w in words:
        if _NON_SPEECH.match(w.text) or normalize_token(w.text) in FILLERS:
            continue
        shown = _display_text(w.text, preset)
        if not shown:
            continue
        if current:
            prev = current[-1]
            if (
                len(current) >= preset.max_words
                or chars + 1 + len(shown) > preset.max_chars
                or w.start - prev.end > 500
                or prev_ended_sentence
                or w.end - current[0].start > 2600
            ):
                chunks.append(CaptionChunk(current[0].start, current[-1].end, current))
                current, chars = [], 0
        current.append(Word(w.start, w.end, shown))
        chars += len(shown) + (1 if chars else 0)
        prev_ended_sentence = bool(_SENTENCE_END.search(w.text))
    if current:
        chunks.append(CaptionChunk(current[0].start, current[-1].end, current))

    # Hold each chunk until the next one starts when the gap is short (no blink), and keep
    # every chunk on screen long enough to read.
    for i, chunk in enumerate(chunks):
        nxt = chunks[i + 1].start if i + 1 < len(chunks) else None
        end = max(chunk.end, chunk.start + 300)
        if nxt is not None:
            end = nxt if nxt - chunk.end < 350 else min(end, nxt)
        chunk.end = end
    return chunks


def clip_chunks(chunks: list[CaptionChunk], start_ms: int, end_ms: int) -> list[CaptionChunk]:
    """Chunks overlapping [start, end), re-timed relative to the clip start."""
    out = []
    for c in chunks:
        if c.end <= start_ms or c.start >= end_ms:
            continue
        words = [
            Word(max(0, w.start - start_ms), min(end_ms, w.end) - start_ms, w.text)
            for w in c.words
            if w.end > start_ms and w.start < end_ms
        ]
        if words:
            out.append(CaptionChunk(max(0, c.start - start_ms), min(end_ms, c.end) - start_ms, words))
    return out


# --- ASS output ---------------------------------------------------------------------------------


def ass_color(hex_color: str, alpha: float = 0.0) -> str:
    """'#RRGGBB' -> '&HAABBGGRR' (ASS stores blue first; alpha 00 = opaque)."""
    h = hex_color.lstrip("#")
    if len(h) != 6 or any(c not in "0123456789abcdefABCDEF" for c in h):
        h = "FFFFFF"
    a = max(0, min(255, round(alpha * 255)))
    return f"&H{a:02X}{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def _inline_color(hex_color: str) -> str:
    return ass_color(hex_color)[4:]  # &HBBGGRR for \c overrides


def ass_escape(text: str) -> str:
    # Braces open override blocks and backslashes start tags; neutralise both so a word
    # in a transcript can never inject styling commands.
    return (
        text.replace("\\", "\u29f5")  # look-alike backslash; no ASS tag can start
        .replace("{", "(")
        .replace("}", ")")
        .replace("\n", " ")
    )


def _ts(ms: int) -> str:
    ms = max(0, int(ms))
    cs = (ms + 5) // 10
    h, rem = divmod(cs, 360_000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def build_ass(
    chunks: list[CaptionChunk],
    preset: CaptionPreset,
    *,
    width: int,
    height: int,
    position: float = 0.72,
    highlight: str | None = None,
    title: str | None = None,
    title_seconds: float = 3.5,
    watermark: str | None = None,
    duration_ms: int | None = None,
) -> str:
    scale = min(width, height) / 1080 * (0.8 if width > height else 1.0)
    size = round(preset.font_size * scale)
    hl = highlight or preset.highlight
    margin = round(width * 0.07)
    x, y = width // 2, round(height * position)
    box_alpha = 1.0 - preset.box_opacity
    border_style = 3 if preset.box else 1
    outline_col = ass_color(preset.box_color, box_alpha) if preset.box else ass_color(preset.outline)
    back_col = ass_color(preset.box_color, box_alpha) if preset.box else ass_color("#000000", 0.45)
    primary = ass_color(hl) if preset.mode == "karaoke" else ass_color(preset.primary)
    secondary = ass_color(preset.primary) if preset.mode == "karaoke" else ass_color(hl)

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Cap,{preset.font_family},{size},{primary},{secondary},{outline_col},{back_col},"
        f"0,0,0,0,100,100,{preset.letter_spacing * scale:.1f},0,{border_style},"
        f"{preset.outline_width * scale:.1f},{preset.shadow * scale:.1f},5,{margin},{margin},0,1",
        f"Style: Title,{TITLE_OVERLAY['font_family']},{round(TITLE_OVERLAY['size'] * scale)},&H00FFFFFF,"
        f"&H00FFFFFF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,{TITLE_OVERLAY['outline'] * scale:.1f},0,8,"
        f"{margin},{margin},{round(height * 0.09)},1",
        f"Style: Mark,{WATERMARK_OVERLAY['font_family']},{round(WATERMARK_OVERLAY['size'] * scale)},"
        f"&H40FFFFFF,&H40FFFFFF,&H80000000,&H00000000,0,0,0,0,100,100,0,0,1,{2 * scale:.1f},0,8,"
        f"{margin},{margin},{round(height * 0.035)},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    pos = f"\\an5\\pos({x},{y})"
    active_tag = f"\\c{_inline_color(hl)}" + ("\\fscx112\\fscy112" if preset.pop else "")

    for chunk in chunks:
        texts = [ass_escape(w.text) for w in chunk.words]
        if preset.mode == "plain":
            lines.append(
                f"Dialogue: 1,{_ts(chunk.start)},{_ts(chunk.end)},Cap,,0,0,0,,{{{pos}}}{' '.join(texts)}"
            )
            continue
        if preset.mode == "karaoke":
            parts = []
            for i, w in enumerate(chunk.words):
                nxt = chunk.words[i + 1].start if i + 1 < len(chunk.words) else chunk.end
                parts.append(f"{{\\kf{max(1, (nxt - w.start) // 10)}}}{texts[i]}")
            lead = max(0, chunk.words[0].start - chunk.start) // 10
            body = (f"{{\\k{lead}}}" if lead else "") + " ".join(parts)
            lines.append(f"Dialogue: 1,{_ts(chunk.start)},{_ts(chunk.end)},Cap,,0,0,0,,{{{pos}}}{body}")
            continue
        for i, w in enumerate(chunk.words):
            ev_start = chunk.start if i == 0 else w.start
            ev_end = chunk.words[i + 1].start if i + 1 < len(chunk.words) else chunk.end
            if ev_end - ev_start < 20:
                continue
            body = " ".join(f"{{{active_tag}}}{t}{{\\r}}" if j == i else t for j, t in enumerate(texts))
            intro = "\\fscx92\\fscy92\\t(0,80,\\fscx100\\fscy100)" if i == 0 and preset.pop else ""
            lines.append(f"Dialogue: 1,{_ts(ev_start)},{_ts(ev_end)},Cap,,0,0,0,,{{{pos}{intro}}}{body}")

    if title:
        end = min(int(title_seconds * 1000), duration_ms or int(title_seconds * 1000))
        lines.append(f"Dialogue: 2,{_ts(0)},{_ts(end)},Title,,0,0,0,,{{\\fad(120,200)}}{ass_escape(title)}")
    if watermark and duration_ms:
        lines.append(f"Dialogue: 0,{_ts(0)},{_ts(duration_ms)},Mark,,0,0,0,,{ass_escape(watermark)}")
    return "\n".join(lines) + "\n"
