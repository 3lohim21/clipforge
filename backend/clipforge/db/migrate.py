"""Forward-only SQL migrations.

Plain .sql files are the schema's source of truth (reviewable, diffable, no ORM magic).
A transaction-scoped advisory lock serialises concurrent migrators, so the API and every
worker can safely run `migrate()` on boot.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import Engine, text

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_LOCK_ID = 7_270_431_001


def migrate(engine: Engine) -> list[str]:
    applied_now: list[str] = []
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": _LOCK_ID})
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " version text PRIMARY KEY,"
                " applied_at timestamptz NOT NULL DEFAULT now())"
            )
        )
        done = {row[0] for row in conn.execute(text("SELECT version FROM schema_migrations"))}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.stem in done:
                continue
            log.info("applying migration", extra={"migration": path.stem})
            conn.exec_driver_sql(path.read_text(encoding="utf-8"))
            conn.execute(text("INSERT INTO schema_migrations (version) VALUES (:v)"), {"v": path.stem})
            applied_now.append(path.stem)
    return applied_now
