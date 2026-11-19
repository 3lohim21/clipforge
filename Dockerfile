# syntax=docker/dockerfile:1.7
# One image, two roles: `clipforge serve` (API + SPA) and `clipforge worker` (media jobs).
# Build from the clipforge/ directory:  docker build -t clipforge .

# ---- 1. frontend --------------------------------------------------------------------------------
FROM node:22-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- 2. base (shared by the CPU and GPU images) -----------------------------------------------
FROM python:3.13-slim-trixie AS base

# ffmpeg (with libass for captions) + fontconfig; tini reaps ffmpeg children and forwards
# SIGTERM so workers can hand unfinished jobs back to the queue.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fontconfig tini ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY backend/requirements.lock ./
RUN pip install -r requirements.lock
COPY backend/ ./backend/
RUN pip install --no-deps ./backend

COPY --from=web /web/dist /app/web

RUN useradd --system --uid 10001 --home /data clipforge \
 && mkdir -p /data && chown clipforge /data
USER clipforge

ENV ENVIRONMENT=production \
    DATA_DIR=/data \
    FRONTEND_DIST=/app/web \
    LOG_JSON=true

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8000/api/healthz', timeout=4)" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "clipforge"]
CMD ["serve", "--workers", "2"]

# ---- 3. gpu worker (optional): docker build --target gpu -t clipforge-gpu . -------------------
# CUDA libraries for CTranslate2 (Whisper) come from PyPI, so no CUDA base image is needed.
# NVENC/NVDEC are provided at runtime by the NVIDIA container toolkit (capabilities: video).
FROM base AS gpu
USER root
RUN pip install "nvidia-cublas-cu12" "nvidia-cudnn-cu12==9.*"
ENV LD_LIBRARY_PATH=/usr/local/lib/python3.13/site-packages/nvidia/cublas/lib:/usr/local/lib/python3.13/site-packages/nvidia/cudnn/lib \
    NVIDIA_DRIVER_CAPABILITIES=compute,video,utility \
    WHISPER_DEVICE=cuda \
    WHISPER_MODEL=large-v3-turbo
USER clipforge
CMD ["worker", "--queues", "transcribe,render"]

# ---- 4. default: CPU image ------------------------------------------------------------------------
FROM base AS runtime
