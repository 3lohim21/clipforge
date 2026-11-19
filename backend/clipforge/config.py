"""Runtime configuration, loaded once from the environment (and an optional .env file).

Every knob has a production-safe default so a fresh install only needs DATABASE_URL,
ADMIN_EMAIL and ADMIN_PASSWORD.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = PACKAGE_DIR / "assets"
FONTS_DIR = ASSETS_DIR / "fonts"
MODELS_DIR = ASSETS_DIR / "models"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- core -----------------------------------------------------------------------------
    environment: Literal["development", "production", "test"] = "development"
    database_url: str = "postgresql+psycopg://clipforge:clipforge@localhost:5432/clipforge"
    db_pool_size: int = 10
    db_max_overflow: int = 10
    data_dir: Path = Path("./data")
    log_level: str = "INFO"
    log_json: bool = False

    # --- web / auth -----------------------------------------------------------------------
    public_url: str = "http://localhost:8000"
    # Extra origins allowed to call the API with credentials (e.g. the Vite dev server).
    cors_origins: list[str] = Field(default_factory=list)
    cookie_secure: bool = False
    session_ttl_days: int = 30
    admin_email: str | None = None
    admin_password: str | None = None
    # Directory with the built SPA (frontend/dist). When present the API serves it.
    frontend_dist: Path | None = None
    # When true, media responses are handed to the reverse proxy via X-Accel-Redirect so
    # Caddy/nginx streams the bytes (sendfile, Range) instead of Python.
    media_accel_redirect: bool = False
    media_accel_prefix: str = "/_protected_media"
    metrics_token: str | None = None

    # --- ingest ---------------------------------------------------------------------------
    allow_private_urls: bool = False  # SSRF guard; only enable for local development
    download_max_height: int = 1080
    max_source_duration_s: int = 6 * 3600
    max_upload_bytes: int = 20 * 1024**3
    ytdlp_cookies_file: Path | None = None
    ytdlp_proxy: str | None = None
    ytdlp_concurrent_fragments: int = 4
    # Reuse a platform's own captions (e.g. YouTube auto-captions) instead of running ASR.
    prefer_platform_captions: bool = True

    # --- transcription --------------------------------------------------------------------
    whisper_model: str = "small"
    whisper_device: Literal["auto", "cpu", "cuda"] = "auto"
    whisper_compute_type: str = "auto"  # int8 on CPU, float16 on CUDA
    whisper_beam_size: int = 1  # greedy decoding: ~2x faster on CPU, small accuracy cost
    whisper_cpu_threads: int = 0  # 0 = let CTranslate2 decide

    # --- analysis -------------------------------------------------------------------------
    # Optional AI re-ranking/titling. Unset key = free heuristic engine only.
    # Cheaper alternatives: claude-sonnet-5-5, claude-haiku-5-5 (see docs/ARCHITECTURE.md).
    anthropic_api_key: str | None = None
    llm_model: str = "claude-opus-5-5"
    llm_max_candidates: int = 40

    # --- rendering ------------------------------------------------------------------------
    render_encoder: Literal["auto", "libx264", "h264_nvenc"] = "auto"
    render_preset: str = "veryfast"
    render_crf: int = 20
    render_audio_normalize: bool = True
    reframe_sample_fps: float = 4.0

    # --- worker ---------------------------------------------------------------------------
    worker_queues: list[str] = Field(
        default_factory=lambda: ["ingest", "transcribe", "analyze", "render", "maintenance"]
    )
    worker_concurrency: int = 2
    job_heartbeat_s: int = 15
    job_stale_after_s: int = 120
    job_poll_interval_s: float = 2.0
    job_retention_days: int = 14

    @field_validator("database_url")
    @classmethod
    def _normalize_db_url(cls, v: str) -> str:
        # Accept the URL formats most hosts hand out and pin the psycopg3 driver.
        if v.startswith("postgres://"):
            v = "postgresql://" + v.removeprefix("postgres://")
        if v.startswith("postgresql://"):
            v = "postgresql+psycopg://" + v.removeprefix("postgresql://")
        return v

    @field_validator("cors_origins", "worker_queues", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str):
            stripped = v.strip()
            if stripped.startswith("["):
                return v  # JSON list; let pydantic parse it
            return [part.strip() for part in stripped.split(",") if part.strip()]
        return v

    # --- derived paths --------------------------------------------------------------------
    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def models_cache_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
