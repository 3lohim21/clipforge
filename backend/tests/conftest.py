"""Shared test fixtures.

Integration tests run against a real PostgreSQL database (TEST_DATABASE_URL) because the
job queue depends on Postgres semantics (SKIP LOCKED, LISTEN/NOTIFY, partial indexes).
"""

from __future__ import annotations

import functools
import http.server
import os
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

_DATA_DIR = tempfile.mkdtemp(prefix="clipforge-test-")
os.environ.update(
    {
        "ENVIRONMENT": "test",
        "DATABASE_URL": os.environ.get(
            "TEST_DATABASE_URL", "postgresql://clipforge:clipforge@localhost:5432/clipforge_test"
        ),
        "DATA_DIR": _DATA_DIR,
        "ALLOW_PRIVATE_URLS": "true",  # the media server below lives on 127.0.0.1
        "ADMIN_EMAIL": "owner@example.com",
        "ADMIN_PASSWORD": "correct-horse-battery",
        "ANTHROPIC_API_KEY": "",
        "WORKER_CONCURRENCY": "1",
        "LOG_LEVEL": "WARNING",
        "RENDER_PRESET": "ultrafast",
        "REFRAME_SAMPLE_FPS": "2",
    }
)

from clipforge.config import get_settings  # noqa: E402

get_settings.cache_clear()

FIXTURES = Path(__file__).parent / "fixtures"
ADMIN = ("owner@example.com", "correct-horse-battery")


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


@pytest.fixture(scope="session")
def media_dir() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="clipforge-media-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(scope="session")
def sample_video(media_dir: Path) -> Path:
    """40 s 1280x720 H.264/AAC 'talk': test pattern + tone (faststart deliberately off)."""
    out = media_dir / "talk.mp4"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=1280x720:rate=25:duration=40",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=220:duration=40",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(out),
    )
    return out


@pytest.fixture(scope="session")
def webm_video(media_dir: Path) -> Path:
    """VP9/Opus WebM: not browser-safe for every target, exercises the proxy path."""
    out = media_dir / "clip.webm"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=640x360:rate=25:duration=6",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=330:duration=6",
        "-c:v",
        "libvpx-vp9",
        "-deadline",
        "realtime",
        "-cpu-used",
        "8",
        "-b:v",
        "300k",
        "-c:a",
        "libopus",
        "-shortest",
        str(out),
    )
    return out


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture(scope="session")
def media_server(media_dir: Path, sample_video: Path, webm_video: Path) -> Iterator[str]:
    handler = functools.partial(_QuietHandler, directory=str(media_dir))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def clean_db() -> Iterator[None]:
    from sqlalchemy import text

    from clipforge.db import get_engine
    from clipforge.db.migrate import migrate

    engine = get_engine()
    migrate(engine)
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE workspaces, jobs RESTART IDENTITY CASCADE"))
    from clipforge.api.deps import auth_cache

    auth_cache._data.clear()
    yield


@pytest.fixture
def client(clean_db: None) -> Iterator:  # type: ignore[type-arg]
    from fastapi.testclient import TestClient

    from clipforge.api.app import create_app

    with TestClient(create_app(), base_url="http://testserver") as c:
        yield c


@pytest.fixture
def auth_client(client):  # type: ignore[no-untyped-def]
    r = client.post("/api/auth/login", json={"email": ADMIN[0], "password": ADMIN[1]})
    assert r.status_code == 200, r.text
    return client


FAKE_SCRIPT = (
    "Here's why most people never get rich. They spend everything they make the moment it lands. "
    "You need to invest early and stay patient for years. "
    "And um so yeah that was the thing. Okay. Right. "
    "What if I told you the biggest mistake is waiting for the perfect moment? "
    "I remember the day I lost a million dollars in one trade. It was insane, I couldn't sleep for a week. "
    "So the lesson is simple. Never risk money you can't afford to lose."
)


def fake_words(duration_s: float, script: str = FAKE_SCRIPT):  # type: ignore[no-untyped-def]
    """Deterministic 'transcript': the script repeated across the whole duration, with
    natural pauses after sentences. Stands in for Whisper (no model download in CI)."""
    from clipforge.transcription.words import Word

    words, t = [], 300
    tokens = script.split()
    i = 0
    while t < duration_s * 1000 - 600:
        tok = tokens[i % len(tokens)]
        words.append(Word(t, t + 260, tok, 0.95))
        t += 330 + (650 if tok.endswith((".", "?", "!")) else 0)
        i += 1
    return words


@pytest.fixture
def fake_whisper(monkeypatch: pytest.MonkeyPatch) -> None:
    from clipforge.transcription import whisper

    def transcribe_file(audio_path, *, settings, duration_s, language=None, on_progress=None, **_):  # type: ignore[no-untyped-def]
        if on_progress:
            on_progress(1.0)
        return fake_words(duration_s), "en"

    monkeypatch.setattr(whisper, "transcribe_file", transcribe_file)


@pytest.fixture
def run_jobs():  # type: ignore[no-untyped-def]
    """Run every queued job (and the jobs they enqueue) synchronously."""
    from clipforge.jobs.worker import Worker
    from clipforge.pipeline.registry import build_registry

    def _run(max_jobs: int = 50) -> int:
        worker = Worker(build_registry(), queues=["ingest", "transcribe", "analyze", "render", "maintenance"])
        return worker.run_until_idle(max_jobs)

    return _run
