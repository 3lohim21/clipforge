"""Caption styles and output layouts — the single source of truth.

The API serves these definitions to the editor, which renders its live preview from the
same numbers the renderer burns into the video (font, size, colours, position, words
per caption), so what the editor shows is what gets exported.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

CaptionMode = Literal["highlight", "karaoke", "plain"]


@dataclass(frozen=True)
class CaptionPreset:
    id: str
    name: str
    font_family: str  # family name as libass/fontconfig sees it
    font_file: str  # file in assets/fonts (also served to the browser)
    font_size: int  # px at a 1080-px-wide frame
    uppercase: bool
    strip_punctuation: bool
    primary: str  # text colour
    highlight: str  # active-word colour
    outline: str
    outline_width: float
    shadow: float
    max_words: int
    max_chars: int
    mode: CaptionMode
    pop: bool  # enlarge the active word
    box: bool  # opaque box behind text
    box_color: str = "#000000"
    box_opacity: float = 0.6
    letter_spacing: float = 0.0


@dataclass(frozen=True)
class Layout:
    id: str
    name: str
    width: int
    height: int
    description: str


CAPTION_PRESETS: dict[str, CaptionPreset] = {
    p.id: p
    for p in [
        CaptionPreset(
            id="bold",
            name="Bold Pop",
            font_family="Poppins Black",
            font_file="Poppins-Black.ttf",
            font_size=78,
            uppercase=True,
            strip_punctuation=True,
            primary="#FFFFFF",
            highlight="#FFE600",
            outline="#000000",
            outline_width=6,
            shadow=0,
            max_words=3,
            max_chars=18,
            mode="highlight",
            pop=True,
            box=False,
        ),
        CaptionPreset(
            id="beast",
            name="Beast",
            font_family="Bangers",
            font_file="Bangers-Regular.ttf",
            font_size=104,
            uppercase=True,
            strip_punctuation=True,
            primary="#FFFFFF",
            highlight="#22E36B",
            outline="#000000",
            outline_width=7,
            shadow=4,
            max_words=2,
            max_chars=14,
            mode="highlight",
            pop=True,
            box=False,
            letter_spacing=1.5,
        ),
        CaptionPreset(
            id="impact",
            name="Impact",
            font_family="Anton",
            font_file="Anton-Regular.ttf",
            font_size=96,
            uppercase=True,
            strip_punctuation=True,
            primary="#FFFFFF",
            highlight="#FF3B30",
            outline="#000000",
            outline_width=5,
            shadow=0,
            max_words=3,
            max_chars=16,
            mode="highlight",
            pop=False,
            box=False,
        ),
        CaptionPreset(
            id="karaoke",
            name="Karaoke",
            font_family="Bebas Neue",
            font_file="BebasNeue-Regular.ttf",
            font_size=108,
            uppercase=True,
            strip_punctuation=True,
            primary="#FFFFFF",
            highlight="#00D2FF",
            outline="#000000",
            outline_width=5,
            shadow=0,
            max_words=4,
            max_chars=22,
            mode="karaoke",
            pop=False,
            box=False,
            letter_spacing=1.0,
        ),
        CaptionPreset(
            id="clean",
            name="Clean",
            font_family="Poppins ExtraBold",
            font_file="Poppins-ExtraBold.ttf",
            font_size=62,
            uppercase=False,
            strip_punctuation=False,
            primary="#FFFFFF",
            highlight="#4DA3FF",
            outline="#000000",
            outline_width=3,
            shadow=2,
            max_words=5,
            max_chars=28,
            mode="highlight",
            pop=False,
            box=False,
        ),
        CaptionPreset(
            id="minimal",
            name="Subtitle box",
            font_family="Poppins ExtraBold",
            font_file="Poppins-ExtraBold.ttf",
            font_size=50,
            uppercase=False,
            strip_punctuation=False,
            primary="#FFFFFF",
            highlight="#FFFFFF",
            outline="#000000",
            outline_width=14,
            shadow=0,
            max_words=7,
            max_chars=38,
            mode="plain",
            pop=False,
            box=True,
            box_opacity=0.65,
        ),
    ]
}
DEFAULT_PRESET = "bold"

LAYOUTS: dict[str, Layout] = {
    lay.id: lay
    for lay in [
        Layout(
            "vertical_fill",
            "Vertical · auto-reframe",
            1080,
            1920,
            "9:16 crop that follows the speaker's face. Best for podcasts and talking heads.",
        ),
        Layout(
            "vertical_fit",
            "Vertical · full frame",
            1080,
            1920,
            "Whole frame centred over a blurred fill. Best for gameplay, screens, wide shots.",
        ),
        Layout("square_fill", "Square · auto-reframe", 1080, 1080, "1:1 face-tracked crop for feeds."),
        Layout("landscape", "Landscape 16:9", 1920, 1080, "Original framing for YouTube and X."),
    ]
}
DEFAULT_LAYOUT = "vertical_fill"


def get_preset(preset_id: str | None) -> CaptionPreset:
    return CAPTION_PRESETS.get(preset_id or DEFAULT_PRESET, CAPTION_PRESETS[DEFAULT_PRESET])


def get_layout(layout_id: str | None) -> Layout:
    return LAYOUTS.get(layout_id or DEFAULT_LAYOUT, LAYOUTS[DEFAULT_LAYOUT])


def catalog() -> dict[str, Any]:
    return {
        "caption_presets": [asdict(p) for p in CAPTION_PRESETS.values()],
        "layouts": [asdict(lay) for lay in LAYOUTS.values()],
        "defaults": {"caption_preset": DEFAULT_PRESET, "layout": DEFAULT_LAYOUT, "caption_position": 0.72},
    }
