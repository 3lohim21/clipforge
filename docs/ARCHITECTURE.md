# ClipForge architecture

ClipForge turns long-form video (podcasts, streams, interviews) into short vertical clips:
paste a link → it downloads, transcribes, finds the most clippable moments, and lets an
editor review, trim, restyle and export 9:16 MP4s with animated captions — all on hardware
you own, with no per-minute fees.

---

## 1. System overview

```
                         Browser (React SPA, one <video> decoder per editor session)
                                 │  HTTPS: JSON API + media (HTTP Range)
                                 ▼
 ┌───────────────────────── Caddy (TLS, zstd/gzip, security headers) ──────────────────────────┐
 │  /api/* , SPA ──────────────► API (FastAPI, stateless, N replicas)                          │
 │  media: API authorises, answers `X-Accel-Redirect` ──► Caddy file_server streams the bytes  │
 └──────────────────────────────────────────────┬──────────────────────────────────────────────┘
                                                │ SQL (pooled)            ▲ shared media volume (ro)
                                                ▼                         │
                                   ┌────────────────────────┐            │
                                   │ PostgreSQL 16           │            │
                                   │ • business data         │            │
                                   │ • job queue (SKIP LOCKED│            │
                                   │   + LISTEN/NOTIFY)      │            │
                                   │ • sessions              │            │
                                   └───────────┬────────────┘            │
                                   claim / heartbeat / complete           │
                                               ▼                          │
             ┌──────────────── Workers (same image, N replicas, per-queue pools) ─────────────┐
             │ ingest: yt-dlp, ffprobe, faststart/proxy, audio+loudness, captions, heatmap    │
             │ transcribe: faster-whisper (CPU int8 / GPU fp16 batched)                         │
             │ analyze: viral-moment engine (+ optional Claude re-rank), face tracks            │
             │ render: one ffmpeg pass (reframe, blur-fill, libass captions, loudnorm, x264/NVENC)│
             │ maintenance: media cleanup, posted-clip view refresh                              │
             └────────────────────────────────────── writes ──► media volume (/data/media) ───┘
```

**Why this shape.** Video work is slow, CPU/GPU-heavy and bursty; HTTP must stay fast. So
the API never touches media bytes or runs ffmpeg — it only reads/writes rows and enqueues
jobs. Workers do all heavy lifting and scale independently. Everything stateful lives in
two places: PostgreSQL and the media volume.

| Component | Tech | Scales by |
|---|---|---|
| Edge | Caddy 2 (auto-HTTPS, HTTP/3, sendfile) | 1 per host / CDN later |
| API | Python 3.13, FastAPI, SQLAlchemy 2, psycopg 3 | uvicorn workers × replicas (stateless) |
| Queue | PostgreSQL table + `FOR UPDATE SKIP LOCKED` + `LISTEN/NOTIFY` | partial indexes; Postgres handles far more jobs/s than video work needs |
| Workers | yt-dlp, ffmpeg 6/7 + libass, faster-whisper, OpenCV (YuNet) | replicas per queue; GPU box for `transcribe`/`render` |
| UI | React 19, React Router 7, TanStack Query 5, Vite 8, hand-written CSS | static files, cache-forever hashed assets |

---

## 2. The pipeline (data flow)

```
POST /api/projects/{id}/videos {urls}
   └─ INSERT videos(status=queued) + INSERT jobs(video.ingest)   ← one transaction
video.ingest      SSRF check → yt-dlp (H.264/AAC preferred, parallel fragments, optional time range)
                  → ffprobe → playable copy (faststart remux, or 720p proxy for exotic codecs)
                  → poster → one decode pass → 16 kHz FLAC + loudness/100 ms
                  → YouTube auto-captions (word-timed json3) + "Most replayed" heatmap + chapters
                  → transcript ready? enqueue analyze : enqueue transcribe          ← one transaction
video.transcribe  faster-whisper, VAD, word timestamps → transcript.json → enqueue analyze
video.analyze     moment engine → [Claude re-rank] → face tracks per clip → clips(status=suggested)
                  → video.status=ready
POST /api/clips/{id}/render
   └─ INSERT renders(spec snapshot) + INSERT jobs(clip.render, priority 10)
clip.render       reframe track (stored or computed) → ASS captions → ffmpeg → renders/{id}.mp4 + .jpg
```

Every stage is **idempotent** (re-runs overwrite their own outputs) and finishes by
updating its row and enqueueing the next stage **in the same transaction**, so a crash
can never strand a video between stages.

### Job queue semantics (`clipforge/jobs/queue.py`)

* **Claim:** `UPDATE jobs … WHERE id = (SELECT … FOR UPDATE SKIP LOCKED LIMIT 1)` — each job
  goes to exactly one worker; no contention between workers.
* **Wake-up:** `pg_notify` on enqueue (delivered at commit) → workers `LISTEN`; pickup in
  milliseconds, polling is only a fallback.
* **Leases:** running jobs heartbeat every 15 s; a reaper re-queues jobs whose worker died
  (or fails them when attempts are exhausted, running the stage's failure hook).
* **Ownership guard:** every transition checks `locked_by`, so a worker that lost its lease
  can never overwrite the new owner's result.
* **Retries:** exponential backoff with jitter; `PermanentJobError` (private video, bad file)
  fails fast with a human message.
* **Dedupe:** partial unique index on `dedupe_key` for live jobs → at most one ingest per
  video, one stats refresh at a time.
* **Graceful shutdown:** SIGTERM → stop claiming, finish within the grace period, hand
  unfinished jobs back without burning an attempt.

---

## 3. Finding viral moments (`clipforge/analysis/`)

The free, deterministic engine (`scorer.py`) is the default; an LLM pass is optional.

1. **Sentences.** Whisper output is punctuated; YouTube auto-captions are not. The
   segmenter detects which and splits on punctuation, or on pauses + a length cap.
   Clips only ever start/end on these boundaries — complete thoughts.
2. **Candidates.** Every run of consecutive sentences whose duration is within the project's
   `[min, max]` clip length (default 15–60 s).
3. **Signals (0..1 each):**

   | Signal | What it measures |
   |---|---|
   | hook | question / hook phrase ("here's why", "the biggest mistake"…), numbers, direct address; penalises mid-thought openers ("and…", "he said…") and starts glued to the previous sentence |
   | ending | lands on a complete thought: terminal punctuation, a pause, no dangling "and/so/the" |
   | energy | per-frame loudness vs. the video's own baseline + share of loud peaks (laughter, hype) |
   | pace | words/second in the engaging band; dead air penalised |
   | emotion | density of emotional, story ("I remember…") and laughter tokens |
   | clarity | few filler words |
   | duration | closeness to the target length |
   | heat | YouTube "Most replayed" audience heatmap, when the platform exposes it |

4. **Score** = weighted sum (weights renormalise when a signal is unavailable) → shown 0–100.
5. **Selection:** greedy non-maximum suppression (≤15 % overlap), N ≈ 1 clip per 4 minutes
   (3–30), never re-suggesting ranges the team already saved.

Range aggregates use prefix sums, so each candidate costs O(1): **a 3-hour podcast
(26k words, 3.4k sentences) is ranked in 0.32 s** (measured on 4 vCPU).

**Optional Claude re-rank (`llm.py`).** Only the shortlist (≤40 windows, ~8–10k tokens) is
sent — not the transcript — so cost is per video, not per minute. Structured outputs
guarantee JSON; server-side refusal fallbacks are enabled; any failure falls back to the
heuristic order. It also writes titles, on-screen hooks and hashtags. Transcript text is
passed as untrusted, delimited data.

---

## 4. Rendering (`clipforge/rendering/`)

* **Auto-reframe (`reframe.py`).** Decode only the clip range at 4 fps, 640 px wide → YuNet
  face detection → active-speaker estimate (mouth motion minus head motion) → per-frame
  target → **shots with hysteresis** (cut only when a new subject persists ≥ 1 s; jitter is
  ignored; no flash cut on the first frame). Two people who fit in the frame are framed
  together. Output: a few `(t, x)` keyframes stored on the clip, turned into a piecewise
  ffmpeg `crop` expression.
* **Captions (`captions.py`).** Words → short chunks (style-specific word/char limits,
  sentence and pause breaks, fillers dropped) → ASS script: one event per spoken word,
  active word recoloured/popped (or `\kf` karaoke), contiguous events (no flicker). Text is
  escaped so transcript content can never inject ASS override tags.
* **One ffmpeg pass (`compose.py`):** input seek → `crop`/blur-fill/letterbox → `ass` (bundled
  OFL fonts via `fontsdir`) → `loudnorm` to −14 LUFS → x264 `veryfast` CRF 20 (or NVENC when a
  GPU is present) → `+faststart`. Blur-fill blurs at ¼ resolution (~16× fewer pixels).
  **A 35 s 1080×1920 export takes ~7 s on 4 vCPU.**
* **WYSIWYG.** Caption styles live in one place (`presets.py`) and are served to the editor
  together with each font's libass size ratio (read from its OS/2 table), so the browser
  preview uses the same fonts, sizes, positions and word grouping as the burned-in export.

Layouts: `vertical_fill` (9:16 face-tracked), `vertical_fit` (full frame over blurred fill),
`square_fill` (1:1), `landscape` (16:9).

---

## 5. Data model

Source of truth: [`backend/clipforge/db/migrations/0001_initial.sql`](../backend/clipforge/db/migrations/0001_initial.sql).

```
workspaces 1─* users 1─* sessions
workspaces 1─* projects 1─* videos 1─* clips 1─* renders
                                          clips 1─* posts
jobs (queue; references entities through its payload)
```

| Table | Purpose / notable columns |
|---|---|
| `workspaces` | tenant (one agency today; SaaS-ready: every row is workspace-scoped) |
| `users` | `role` owner/admin/editor; scrypt password hash; unique `lower(email)` |
| `sessions` | opaque cookie tokens, **SHA-256 only stored**, sliding expiry |
| `projects` | one client/campaign; `settings` jsonb = brand kit + clip rules + CPM |
| `videos` | source + pipeline `status`/`progress`/`error`; `assets` jsonb = storage keys; `meta` = platform metadata, chapters, analysis summary |
| `clips` | time range + `edit` jsonb (layout, caption style, framing, stored face track); `origin` ai/manual; `status` suggested/saved/discarded; score + breakdown |
| `renders` | immutable export; `spec` jsonb snapshots every setting at request time |
| `posts` | where a clip was published + views/likes (refreshed every 6 h) → revenue estimate |
| `jobs` | durable queue (see §2) |

Indexing: every foreign key used for listing has a `(parent_id, created_at DESC)` or
`(parent_id, start_ms)` index; the queue has partial indexes over only runnable / running
rows. IDs are **UUIDv7** (time-ordered → append-only B-tree inserts at scale).

Large/derived data stays **out of the database**: transcripts, loudness curves, posters,
audio and renders are files keyed by `videos/<id>/…` and `renders/<id>.mp4`.

---

## 6. HTTP API

All JSON under `/api`, cookie-session auth (HttpOnly, SameSite=Lax), interactive docs at
`/api/docs`. Every entity lookup is scoped to the caller's workspace (`services/access.py`).

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/auth/login` · `/logout` · `/password` | session lifecycle (login is rate-limited) |
| GET | `/api/auth/me` | current user + workspace |
| GET/POST/DELETE | `/api/users[/{id}]` | team (admins manage) |
| GET/POST | `/api/projects` | list (with stats in 4 grouped queries) / create |
| GET/PATCH/DELETE | `/api/projects/{id}` | project + brand kit; delete cascades (+ delayed media cleanup) |
| GET/POST | `/api/projects/{id}/videos` | list / import up to 50 links (optional time range) |
| PUT | `/api/projects/{id}/videos/upload?filename=` | raw-body streaming upload |
| GET/DELETE | `/api/videos/{id}` | status, progress, metadata |
| POST | `/api/videos/{id}/retry` · `/reanalyze` | resume from the failed stage · re-run moments |
| GET | `/api/videos/{id}/transcript` · `/timeline` · `/captions?preset=` | editor data |
| GET | `/api/videos/{id}/media/{playback\|poster}` | Range-capable media |
| GET/POST | `/api/videos/{id}/clips` | clips of a video / create manual clip |
| PATCH/DELETE | `/api/clips/{id}` | trim, retitle, restyle, keep/discard |
| POST | `/api/clips/{id}/render` | queue an export (double-click safe) |
| GET | `/api/clips/{id}/renders` · `/api/renders/{id}` | export status |
| GET | `/api/renders/{id}/file[?download=1]` · `/thumbnail` | the MP4 / its thumbnail |
| GET | `/api/projects/{id}/clips?status=` · `/renders` | project library |
| GET/POST/DELETE | `/api/projects/{id}/posts` · `/api/clips/{id}/posts` · `/api/posts/{id}` | view tracking |
| POST | `/api/posts/{id}/refresh` | refresh one post's stats now |
| GET | `/api/presets` · `/api/fonts/{file}` | caption styles, layouts, font metrics, fonts |
| GET | `/api/healthz` · `/api/readyz` · `/metrics` | liveness · readiness · Prometheus |

Long operations return immediately (`201`/`202`) with a row whose `status`/`progress` the
UI polls — only while something is actually processing.

---

## 7. Frontend architecture (`frontend/src/`)

```
api/        types.ts (contract), client.ts (fetch + XHR upload with progress), queries.ts (TanStack Query hooks)
lib/        player.ts (PlayerStore), crop.ts (mirrors the renderer), fonts.ts, format.ts
components/ AppShell (auth gate, top bar), ui (badges, modal, toasts), icons (inline SVG)
pages/      Login, Projects (dashboard + KPIs), Project (videos/clips/posts/settings), Team
workspace/  Workspace (editor page + keyboard), Player, Timeline (canvas), MomentsPanel,
            TranscriptPanel, ClipEditor (live 9:16 preview, captions overlay, exports)
```

Performance decisions:

* **One decoder.** The editor has a single `<video>`; the 9:16 preview draws its frames onto
  a canvas (crop / blur-fill / letterbox like the renderer) instead of decoding twice.
* **No 60 fps React renders.** `PlayerStore` is an external store; components subscribe to
  selected slices (`usePlayer(s => Math.floor(s.timeMs/100))` re-renders 10×/s). The
  timeline canvas, transcript highlight and crop guide update imperatively.
* **Timeline** caches waveform/heatmap/ruler in an offscreen layer; per-frame redraw only
  blits it and draws clips + playhead.
* **Transcript** (30k+ words) renders once; `content-visibility: auto` skips off-screen
  paragraphs and the playing word is a single class toggle.
* **Network.** Server state lives in TanStack Query; lists poll only while work is in
  flight; clip edits are optimistic; transcripts/timelines/captions are cached forever per
  video. The editor bundle is code-split (13 KB gzip); initial load ≈ 120 KB gzip.

---

## 8. Caching strategy

| What | Where | Policy |
|---|---|---|
| Hashed JS/CSS | browser / CDN | `immutable`, 1 year |
| Fonts, posters, render files, thumbnails | browser | `private, immutable` (content never changes for a given id) |
| Playback video | browser | `private, max-age=86400` + Range |
| Transcript / timeline / captions JSON | TanStack Query | `staleTime: Infinity` per video |
| Auth session → user | API process memory | 30 s TTL (evicted on logout / password change) |
| Whisper model | worker process | loaded once, shared by all jobs |
| Face tracks | `clips.edit.track` | computed during analysis or first export, reused while the range is unchanged |
| yt-dlp extractor cache | `/data/cache/yt-dlp` | persisted signature functions |
| Platform captions | — | reused instead of running ASR (zero transcription cost for most YouTube videos) |

A shared cache (Redis) is deliberately absent: nothing hot enough needs it at this scale,
and Postgres already provides the queue. Add one when the API runs many replicas and the
30 s auth cache hit-rate drops.

---

## 9. Security

* **SSRF:** links are syntax-checked at submit and re-resolved by the worker right before
  download; every resolved address must be globally routable (no RFC1918, loopback,
  link-local/metadata, ULA, IPv4-mapped tricks). Deploy workers on a network without
  routes to internal services for defence in depth.
* **Auth:** scrypt (memory-hard) password hashes; opaque random session tokens stored as
  SHA-256; HttpOnly + SameSite=Lax + Secure cookies; constant-time login failure path;
  per-IP login throttling; cross-origin writes rejected by an Origin check.
* **Tenancy:** every lookup goes through `scoped()` — a foreign ID is indistinguishable from
  a missing one (404).
* **Injection:** subprocesses use argv lists (no shell); storage keys are generated and
  validated against traversal; ASS caption text is escaped; filenames sanitised in
  `Content-Disposition`.
* **Containers:** non-root user, tini as PID 1, read-only media mount for the edge proxy.

---

## 10. Key decisions & tradeoffs

| Decision | Alternative | Why |
|---|---|---|
| Postgres as the job queue | Redis + Celery/RQ | transactional enqueue, one less stateful service, ample throughput for minutes-long jobs |
| Python for API + workers | Node API + Python workers | one language for the domain model; ML/media ecosystem is Python |
| Local faster-whisper by default, platform captions first | paid ASR APIs | zero marginal cost; captions make most YouTube imports instant |
| Heuristic moment engine + optional LLM | LLM-only | works free and offline; LLM improves titles/ranking for pennies/video when enabled |
| ffmpeg + libass single pass | Remotion / browser rendering | fastest server-side path; GPU-ready; deterministic |
| SPA + JSON API | Next.js SSR | logged-in tool, no SEO; static hosting; simpler ops |
| Local volume storage | S3 from day one | single-server MVP; storage interface is narrow (see §11) |
| Polling for progress | SSE/WebSockets | trivially robust behind any proxy; polling only while work is in flight |

---

## 11. Scaling path

1. **Single server (today).** 8 vCPU / 16–32 GB / NVMe: compose with `--scale worker=N`.
2. **GPU worker.** Add a GPU host serving `transcribe,render` (`docker-compose.gpu.yml`):
   large-v3-turbo transcribes an hour of audio in ~1–2 min; NVENC renders several clips
   in parallel. CPU hosts keep `ingest,analyze,maintenance`.
3. **Object storage.** Replace `LocalStorage` with an S3/R2 adapter (same 6-method surface:
   resolve, exists, size, read/write JSON, delete prefix): workers pull inputs to local
   scratch, push outputs; the API returns short-lived presigned URLs instead of
   X-Accel-Redirect, and a CDN serves media. Managed Postgres with PgBouncer.
4. **Kubernetes.** API Deployment + HPA on CPU; worker Deployments per queue autoscaled by
   KEDA's PostgreSQL scaler on `SELECT count(*) FROM jobs WHERE queue='render' AND status='queued'`
   (also exported as `clipforge_queue_jobs`).
5. **SaaS.** Workspaces already scope every row; add workspace membership, plans/quotas
   (per-workspace job fairness via the `jobs.workspace_id` column) and billing.

Next optimisations worth doing when volume grows: a slim API image without the ML stack
(~1 GB smaller), word-level transcript editing, live-chat-replay activity as a moment signal,
split-screen (facecam + gameplay) layout for streamers, scheduled auto-posting.
