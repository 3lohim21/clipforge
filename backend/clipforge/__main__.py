"""Command line entry point.

clipforge serve [--host 0.0.0.0 --port 8000 --workers 2]   HTTP API (+ SPA if built)
clipforge worker [--queues ingest,render --concurrency 2]   job worker
clipforge migrate                                           apply DB migrations
clipforge create-user EMAIL [--role admin] [--name NAME]    add a teammate (prompts for password)
"""

from __future__ import annotations

import argparse
import getpass
import sys

from clipforge.config import get_settings


def _serve(args: argparse.Namespace) -> None:
    import uvicorn

    uvicorn.run(
        "clipforge.api.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        workers=args.workers,
        proxy_headers=True,
        forwarded_allow_ips="*",
        access_log=False,
        timeout_graceful_shutdown=20,
    )


def _worker(args: argparse.Namespace) -> None:
    from clipforge.db import get_engine
    from clipforge.db.migrate import migrate
    from clipforge.jobs.worker import Worker
    from clipforge.logging import configure_logging
    from clipforge.pipeline.registry import build_registry, periodic_tasks

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    migrate(get_engine())
    queues = [q.strip() for q in args.queues.split(",")] if args.queues else settings.worker_queues
    if args.metrics_port:
        from prometheus_client import start_http_server

        start_http_server(args.metrics_port)
    Worker(
        build_registry(), queues=queues, concurrency=args.concurrency, periodic=periodic_tasks(queues)
    ).run()


def _migrate(_: argparse.Namespace) -> None:
    from clipforge.db import get_engine
    from clipforge.db.migrate import migrate

    print("applied:", migrate(get_engine()) or "nothing (up to date)")


def _create_user(args: argparse.Namespace) -> None:
    from sqlalchemy import func, select

    from clipforge.core.security import hash_password
    from clipforge.db import get_engine, session_scope
    from clipforge.db.migrate import migrate
    from clipforge.db.models import User, Workspace

    migrate(get_engine())
    password = getpass.getpass("Password (min 10 chars): ")
    if len(password) < 10:
        sys.exit("Password too short.")
    with session_scope() as s:
        if s.scalar(
            select(func.count()).select_from(User).where(func.lower(User.email) == args.email.lower())
        ):
            sys.exit("A user with that email already exists.")
        workspace = s.scalar(select(Workspace).order_by(Workspace.created_at).limit(1))
        role = args.role
        if workspace is None:
            workspace = Workspace(name="My Agency")
            s.add(workspace)
            s.flush()
            role = "owner"
        s.add(
            User(
                workspace_id=workspace.id,
                email=args.email.lower(),
                name=args.name or "",
                password_hash=hash_password(password),
                role=role,
            )
        )
    print(f"created {role} {args.email}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="clipforge")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serve")
    p.add_argument("--host", default="0.0.0.0")  # noqa: S104 - container entry point
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--workers", type=int, default=1)
    p.set_defaults(fn=_serve)

    p = sub.add_parser("worker")
    p.add_argument("--queues", default=None, help="comma-separated (default: WORKER_QUEUES)")
    p.add_argument("--concurrency", type=int, default=None)
    p.add_argument("--metrics-port", type=int, default=None)
    p.set_defaults(fn=_worker)

    sub.add_parser("migrate").set_defaults(fn=_migrate)

    p = sub.add_parser("create-user")
    p.add_argument("email")
    p.add_argument("--name", default="")
    p.add_argument("--role", choices=["admin", "editor"], default="editor")
    p.set_defaults(fn=_create_user)

    args = parser.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
