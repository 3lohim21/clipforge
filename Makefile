# Developer shortcuts. Local dev needs Python 3.12+, Node 22+, ffmpeg and a PostgreSQL database.
#   make setup                       # install backend + frontend deps
#   make api / make worker / make web  (three terminals) -> http://localhost:5173
.PHONY: setup api worker web test lint fmt build up down logs

PY := backend/.venv/bin
DEV_ENV := ALLOW_PRIVATE_URLS=true CORS_ORIGINS=http://localhost:5173 PUBLIC_URL=http://localhost:5173

setup:
	cd backend && python3 -m venv .venv && .venv/bin/pip install -c requirements.lock -e ".[whisper,llm,dev]"
	cd frontend && npm ci

api:
	cd backend && $(DEV_ENV) .venv/bin/clipforge serve --port 8000

worker:
	cd backend && $(DEV_ENV) .venv/bin/clipforge worker

web:
	cd frontend && npm run dev

test:
	cd backend && .venv/bin/pytest -q
	cd frontend && npm run typecheck && npm test

lint:
	cd backend && .venv/bin/ruff check clipforge tests && .venv/bin/ruff format --check clipforge tests

fmt:
	cd backend && .venv/bin/ruff format clipforge tests && .venv/bin/ruff check --fix clipforge tests

build:
	docker build -t clipforge:latest .

up:
	cd deploy && docker compose up -d --build

down:
	cd deploy && docker compose down

logs:
	cd deploy && docker compose logs -f --tail=100
