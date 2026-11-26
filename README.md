# ClipForge — your own clipping platform

Paste a link to a podcast, stream or interview. ClipForge downloads it, transcribes it,
finds the moments most likely to go viral, and lets your team review, trim and export
vertical clips with animated captions — on your own server, with **no per-minute fees**.

Built for clipping agencies: one project per creator/campaign, a brand kit per client,
keyboard-first review, team accounts, and view tracking that turns campaign CPMs into a
revenue number.

## What it does

| | |
|---|---|
| **Import from anywhere** | YouTube, Twitch & Kick VODs, TikTok, X, Instagram, Vimeo, Rumble, direct `.mp4` links (anything [yt-dlp](https://github.com/yt-dlp/yt-dlp) reads), or drag-and-drop uploads. Up to 50 links at once; optionally only a time range of a long stream. |
| **Fast transcripts, free** | Reuses YouTube's own word-timed captions when available (instant); otherwise local Whisper (`faster-whisper`, CPU or GPU). |
| **Viral moment detection** | Scores every sentence-aligned window on hook strength, payoff, loudness spikes, pacing, emotional/story language, and YouTube's **"Most replayed"** audience heatmap. Each pick gets a 0–100 score and plain-language reasons. Optional Claude pass writes titles, hooks and hashtags. |
| **Auto-reframe to 9:16** | Face tracking with active-speaker detection: the crop follows whoever is talking and cuts like an editor (no jittery panning). Also full-frame-over-blur, 1:1 and 16:9. |
| **Animated captions** | Word-by-word highlight, pop, karaoke and subtitle-box styles in bold display fonts; title hook and watermark overlays. |
| **Editor** | Waveform + heatmap timeline with drag-to-trim (snaps to word boundaries), transcript-based clipping (select words → clip), live 9:16 preview identical to the export, keyboard shortcuts for fast triage. |
| **Exports** | 1080×1920 H.264/AAC MP4, loudness-normalised to −14 LUFS, faststart; ~7 s for a 35 s clip on 4 vCPU (NVENC on GPU). |
| **Agency ops** | Clients/campaigns as projects, per-client brand kits and clip rules, team roles, clip library with copy-ready captions, posted-clip view tracking and CPM revenue estimates. |

## Quick start (production)

```bash
git clone https://github.com/3lohim21/clipforge.git && cd clipforge/deploy
cp .env.example .env      # set DOMAIN, POSTGRES_PASSWORD, ADMIN_EMAIL, ADMIN_PASSWORD
docker compose up -d --build
```

Open `https://<DOMAIN>` and sign in. Full guide: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Local development

Requirements: Python 3.12+, Node 22+, ffmpeg (with libass), PostgreSQL 16.

```bash
createdb clipforge                 # or any Postgres; set DATABASE_URL accordingly
make setup                         # backend venv + frontend deps
ADMIN_EMAIL=me@example.com ADMIN_PASSWORD=dev-password-123 make api   # :8000
make worker                        # second terminal
make web                           # third terminal -> http://localhost:5173
make test                          # 67 backend tests + frontend typecheck/tests
```

Backend tests need a database (`TEST_DATABASE_URL`, default
`postgresql://clipforge:clipforge@localhost:5432/clipforge_test`).
After a deploy, `e2e/smoke.cjs` drives the real UI through import → edit → export (see the file header).

## How it's built

```
clipforge/
├── backend/                    Python 3.13 · FastAPI · SQLAlchemy 2 · psycopg 3
│   ├── clipforge/
│   │   ├── api/                HTTP layer: app factory, routes, schemas, auth deps, media responses
│   │   ├── services/           business operations used by routes (imports, clips, renders, tenancy)
│   │   ├── jobs/               Postgres job queue (SKIP LOCKED, LISTEN/NOTIFY) + worker runtime
│   │   ├── pipeline/           job handlers: ingest → transcribe → analyze, render, maintenance
│   │   ├── media/              ffmpeg/ffprobe wrappers, yt-dlp downloader, audio analysis, caption parsing
│   │   ├── transcription/      word-level transcript model, faster-whisper engine
│   │   ├── analysis/           viral-moment engine, segmenter, lexicon, optional Claude re-ranker
│   │   ├── rendering/          presets (styles/layouts), ASS captions, face-tracked reframe, ffmpeg compose
│   │   ├── storage/            media storage (local volume; S3-ready interface)
│   │   ├── db/                 engine/session, ORM models, SQL migrations (source of truth)
│   │   ├── core/               ids (UUIDv7), security (scrypt, tokens), URL/SSRF guard, project settings
│   │   └── assets/             OFL caption fonts, YuNet face model (MIT)
│   └── tests/                  67 tests incl. end-to-end link → export
├── frontend/                   React 19 · Vite 8 · TanStack Query · React Router
│   └── src/{api,lib,components,pages,workspace}
├── deploy/                     docker-compose (+GPU override), Caddyfile, .env.example
├── docs/                       ARCHITECTURE.md, DEPLOYMENT.md
├── Dockerfile                  one image: `clipforge serve` | `clipforge worker`
└── Makefile
```

Design, data model, API reference, algorithms and scaling path:
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Configuration

Everything is environment variables with safe defaults — see
[`deploy/.env.example`](deploy/.env.example) and
[`backend/clipforge/config.py`](backend/clipforge/config.py). The ones you'll touch:

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | local Postgres | connection string |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | — | first-boot owner account |
| `WHISPER_MODEL` | `small` | `tiny`…`large-v3`; `large-v3-turbo` on GPU |
| `PREFER_PLATFORM_CAPTIONS` | `true` | reuse YouTube captions instead of transcribing |
| `WORKER_QUEUES` / `WORKER_CONCURRENCY` | all / `2` | specialise and size workers |
| `YTDLP_COOKIES_FILE` / `YTDLP_PROXY` | — | get past platform bot checks |
| `ANTHROPIC_API_KEY` / `LLM_MODEL` | — / `claude-opus-5-5` | optional AI titles & re-ranking |
| `RENDER_PRESET` / `RENDER_CRF` / `RENDER_ENCODER` | `veryfast` / `20` / `auto` | export speed vs. quality; NVENC auto-detected |

Per-client settings (caption style, layout, highlight colour, watermark, clip length rules,
language, CPM) live on each project's **Settings** tab.

## Costs

Running cost is your server: there is no per-minute charge. The only optional paid
component is the Claude re-rank (≈ 10k input tokens per video — cents per video, not per
minute); leave `ANTHROPIC_API_KEY` empty to run fully free.

## Using it responsibly

Only clip content you own or have permission to clip (clipping campaigns and creator
agreements grant this). Respect the source platforms' terms. Keep cookies for platform
access on a dedicated account.
