# Deploying ClipForge

The recommended starting point is **one Linux server with Docker Compose**: it is cheap,
easy to operate, and comfortably handles an agency's daily volume. The same image scales
out to more worker hosts, a GPU box, and Kubernetes later (see ARCHITECTURE.md §11).

## 1. Server sizing

| Volume | Server | Notes |
|---|---|---|
| Starting out (≤ 20 h of source/day) | 8 vCPU, 16 GB RAM, 500 GB NVMe | e.g. Hetzner CCX33 / AX42, OVH, DigitalOcean CPU-optimised |
| Growing (≤ 100 h/day) | the above + 1 GPU worker (RTX 4000/L4/4090, 16+ GB VRAM) | GPU takes `transcribe,render` |
| Heavy | several CPU workers + GPU workers + object storage | see ARCHITECTURE.md §11 |

Disk is the resource that runs out first: a 1-hour 1080p source is ~1–2.5 GB plus ~60 MB
of derived files; each export is 5–20 MB. Budget storage for your retention window.

## 2. First deployment

```bash
# on the server (Ubuntu 24.04 / Debian 12+ with Docker Engine + compose plugin)
git clone https://github.com/3lohim21/clipforge.git && cd clipforge/deploy   # private repo: GitHub asks for a token
cp .env.example .env
nano .env            # DOMAIN, POSTGRES_PASSWORD, ADMIN_EMAIL, ADMIN_PASSWORD (+ optional keys)
docker compose up -d --build
docker compose ps    # api healthy, postgres healthy, worker + caddy running
```

1. Point your domain's DNS `A`/`AAAA` record at the server **before** starting, so Caddy can
   obtain the Let's Encrypt certificate automatically. Open ports 80 and 443 only.
2. Visit `https://<DOMAIN>` and sign in with `ADMIN_EMAIL` / `ADMIN_PASSWORD`. Add teammates
   under **Team** (or `docker compose exec api clipforge create-user editor@agency.com`).
3. Scale workers to your CPU count: `docker compose up -d --scale worker=3`
   (each worker runs `WORKER_CONCURRENCY` jobs; renders use all cores, so start with
   workers × concurrency ≈ vCPUs / 4).

Upgrades: `git pull && docker compose up -d --build`. Migrations run automatically on boot
(serialised by an advisory lock); workers finish or hand back running jobs on SIGTERM.

### Using the CI-built image instead of building on the server

Pushes to `main` publish `ghcr.io/<owner>/clipforge:<sha>` and `:latest`. Set
`CLIPFORGE_IMAGE=ghcr.io/<owner>/clipforge:<sha>` in `.env` and run
`docker compose pull && docker compose up -d` for a reproducible, instantly-rollbackable
deploy (roll back by setting the previous sha).

### GPU worker

Install the NVIDIA driver + NVIDIA Container Toolkit, then:

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

The GPU worker serves `transcribe,render` with Whisper `large-v3-turbo` and NVENC (auto-detected;
falls back to x264 if NVENC is unavailable). The CPU worker keeps `ingest,analyze,maintenance`.

## 3. YouTube bot checks (read this before going live)

Datacenter IPs are frequently challenged by YouTube ("Sign in to confirm you're not a bot").
ClipForge surfaces this as a clear error on the video. Mitigations, in order:

1. **Cookies:** export cookies for a throwaway Google account (Netscape format, e.g. with a
   "Get cookies.txt" browser extension), copy to the media volume as `/data/cookies.txt`,
   and set `YTDLP_COOKIES_FILE=/data/cookies.txt`.
2. **Proxy:** set `YTDLP_PROXY` to a residential proxy.
3. Keep yt-dlp current: rebuild the image regularly (the lock pins it; bump it with
   `uv pip compile pyproject.toml --extra whisper --extra llm -o requirements.lock --upgrade-package yt-dlp`).

Twitch/Kick VODs, direct MP4 links and uploads are not affected.

## 4. CI/CD

`.github/workflows/ci.yml` (runs on every push and pull request):

| Job | What it proves |
|---|---|
| backend | `ruff check`, `ruff format --check`, 67 pytest tests against a real Postgres 16 service and real ffmpeg (queue concurrency, SSRF guard, tenancy isolation, face tracking, every render layout, link-to-export end-to-end) |
| frontend | TypeScript strict typecheck, Vitest unit tests, production build |
| image | Docker build (BuildKit cache); on `main` pushes to GHCR |

Recommended branch protection: require `backend` and `frontend` on PRs. Deploy by updating
`CLIPFORGE_IMAGE` (manually, or with a deploy step that SSHes to the server and runs
`docker compose pull && docker compose up -d`).

## 5. Monitoring

**Health:** `GET /api/healthz` (liveness, no dependencies) and `GET /api/readyz`
(database reachable) — wire them to your uptime monitor (UptimeRobot, Better Stack).

**Metrics (Prometheus):** `GET /metrics` on the API (protect with `METRICS_TOKEN`,
`Authorization: Bearer …`); workers expose job metrics on `:9100` (`--metrics-port`).

| Metric | Use |
|---|---|
| `clipforge_queue_jobs{queue,status}` | backlog per queue — the autoscaling signal |
| `clipforge_queue_oldest_seconds{queue,status}` | how long the oldest job has waited |
| `clipforge_jobs_total{kind,outcome}` | succeeded / retry / failed rates |
| `clipforge_job_duration_seconds{kind}` | per-stage latency (ingest, transcribe, render…) |
| `clipforge_http_requests_total{method,route,status}` / `…_duration_seconds` | API RED metrics per route template |

**Suggested alerts**

* `clipforge_queue_oldest_seconds{status="queued"} > 900` for 10 m → workers down or undersized
* `rate(clipforge_jobs_total{outcome="failed"}[30m]) / rate(clipforge_jobs_total[30m]) > 0.2` → platform blocking (cookies!) or a bad release
* 5xx ratio > 1 % for 5 m
* media volume > 80 % full; Postgres connections > 80 % of `max_connections`

**Logs:** every container logs one JSON object per line to stdout (rotated by Docker at
5×20 MB). Ship them with Loki/Promtail, Vector or Datadog; useful fields: `logger`,
`level`, `job_id`, `kind`, `video_id`, `error`, `route`, `ms`.

## 6. Backups & data retention

* **Database (small, critical):** nightly `docker compose exec -T postgres pg_dump -U clipforge -Fc clipforge > clipforge-$(date +%F).dump`, shipped off-box (restic/rclone to S3/B2). Test a restore monthly.
* **Media (large, re-creatable):** sources can be re-downloaded from their links; back up
  `renders/` if exports are your deliverable. Volume snapshots work well.
* Deleting a video/project removes its files via a delayed `storage.cleanup` job.

## 7. Production checklist

- [ ] DNS points at the server; `https://DOMAIN` serves a valid certificate
- [ ] `.env` has strong `POSTGRES_PASSWORD` and `ADMIN_PASSWORD`; `.env` is not in git
- [ ] Only ports 80/443 (and SSH) are open; Postgres is not exposed
- [ ] Signed in, created a project, imported a link, exported a clip end-to-end — automated by
      `BASE=https://DOMAIN EMAIL=… PASSWORD=… VIDEO_URL=… node e2e/smoke.cjs` (Playwright)
- [ ] `YTDLP_COOKIES_FILE` configured if you clip YouTube from a datacenter IP
- [ ] Workers scaled to the machine (`--scale worker=N`); GPU worker if needed
- [ ] `METRICS_TOKEN` set and Prometheus scraping `/metrics` + worker `:9100`
- [ ] Uptime check on `/api/readyz`; alerts for queue age, failure rate, disk usage
- [ ] Nightly `pg_dump` shipped off-box, restore tested
- [ ] `ANTHROPIC_API_KEY` set only if you want AI titles/re-ranking (optional)
- [ ] Team accounts created with least privilege (editors for clippers)
- [ ] You have the rights or the creator's permission to clip the content you import

## 8. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Video fails "bot check" / "Sign in" | see §3 (cookies / proxy) |
| Stuck in "Transcribing" for long | CPU-bound: use a smaller `WHISPER_MODEL`, more workers, or a GPU worker; check `clipforge_queue_oldest_seconds{queue="transcribe"}` |
| Exports slow | lower `RENDER_PRESET` (`superfast`), more render workers, or GPU (NVENC) |
| Player won't seek | check that Caddy serves media (`X-Accel-Redirect`); `MEDIA_ACCEL_REDIRECT=true` requires the Caddy service |
| 403 "Cross-origin request blocked" | `PUBLIC_URL` must match the URL users open (set automatically from `DOMAIN` in compose) |
| Login loop on HTTP | `COOKIE_SECURE=true` requires HTTPS; for local plain-HTTP testing set it to `false` |
