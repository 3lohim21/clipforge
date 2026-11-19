"""Pure unit tests: ids, passwords, URL safety, project settings."""

from __future__ import annotations

import time

import pytest

from clipforge.core.ids import new_id
from clipforge.core.project_settings import ProjectSettings, load_settings
from clipforge.core.security import hash_password, verify_password
from clipforge.core.urls import (
    UnsafeURLError,
    assert_public_host,
    detect_platform,
    parse_media_url,
    split_url_list,
)


def test_uuid7_is_version_7_and_time_ordered_across_milliseconds() -> None:
    a = new_id()
    time.sleep(0.002)
    b = new_id()
    assert a.version == 7 and b.version == 7
    assert a < b
    assert len({new_id() for _ in range(10_000)}) == 10_000


def test_password_hash_roundtrip_and_rejects_garbage() -> None:
    h = hash_password("hunter2-but-longer")
    assert h.startswith("scrypt$")
    assert verify_password("hunter2-but-longer", h)
    assert not verify_password("wrong", h)
    assert not verify_password("x", "not-a-hash")
    assert hash_password("same") != hash_password("same")  # salted


@pytest.mark.parametrize(
    ("url", "platform"),
    [
        ("https://www.youtube.com/watch?v=abc", "youtube"),
        ("https://youtu.be/abc", "youtube"),
        ("https://www.twitch.tv/videos/123", "twitch"),
        ("https://kick.com/x/videos/1", "kick"),
        ("https://www.tiktok.com/@a/video/1", "tiktok"),
        ("https://x.com/a/status/1", "x"),
        ("https://example.com/file.mp4", "web"),
        ("https://notyoutube.com/watch", "web"),
    ],
)
def test_detect_platform(url: str, platform: str) -> None:
    assert detect_platform(url) == platform


def test_parse_media_url_normalises_and_rejects_bad_input() -> None:
    assert parse_media_url("youtube.com/watch?v=1#t=5") == "https://youtube.com/watch?v=1"
    for bad in ("ftp://x.com/a", "javascript:alert(1)", "https://user:pw@x.com/v", "https://"):
        with pytest.raises(UnsafeURLError):
            parse_media_url(bad)


def test_split_url_list_dedupes_and_keeps_order() -> None:
    blob = "https://a.com/1\nhttps://b.com/2, https://a.com/1  https://c.com/3\n\n"
    assert split_url_list(blob) == ["https://a.com/1", "https://b.com/2", "https://c.com/3"]


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data",  # cloud metadata endpoint
        "http://10.0.0.5/",
        "http://[::1]/",
        "http://localhost:5432/",
        "http://[::ffff:127.0.0.1]/",
    ],
)
def test_ssrf_guard_blocks_internal_addresses(url: str) -> None:
    with pytest.raises(UnsafeURLError):
        assert_public_host(url)


def test_ssrf_guard_allows_public_literal_and_dev_override() -> None:
    assert_public_host("http://8.8.8.8/video.mp4")
    assert_public_host("http://127.0.0.1/x", allow_private=True)


def test_project_settings_sanitises_bad_values() -> None:
    s = ProjectSettings(
        caption_preset="nope", layout="nope", highlight_color="red", min_clip_s=30, max_clip_s=20
    )
    assert s.caption_preset == "bold" and s.layout == "vertical_fill" and s.highlight_color is None
    assert s.max_clip_s > s.min_clip_s and s.min_clip_s <= s.target_clip_s <= s.max_clip_s
    assert load_settings({"min_clip_s": "not a number"}) == ProjectSettings()
    assert load_settings({"unknown_key": 1, "watermark": "  "}).watermark is None
