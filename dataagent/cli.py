"""CLI entry points: api, worker, enqueue-daily, migrate, smoke, demo."""

from __future__ import annotations

import argparse
import os
import subprocess
from datetime import date

from dataagent.config import get_settings, reset_settings


def cmd_api(args: argparse.Namespace) -> int:
    settings = get_settings()
    import uvicorn

    uvicorn.run(
        "dataagent.api.app:create_app",
        factory=True,
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
    )
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    from dataagent.worker import run_forever

    run_forever()
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    env = os.environ.copy()
    settings = get_settings()
    env["DATAAGENT_DATABASE_URL"] = settings.database_url
    return subprocess.call(["alembic", "upgrade", "head"], env=env)


def cmd_enqueue_daily(args: argparse.Namespace) -> int:
    from dataagent.jobs.queue import JobQueue
    from dataagent.storage.database import init_engine, session_scope
    from dataagent.storage import models as m
    from sqlalchemy import select

    settings = get_settings()
    init_engine(settings.database_url)
    local_date = date.fromisoformat(args.date) if args.date else date.today()
    with session_scope() as session:
        projects = list(session.execute(select(m.Project)).scalars())
        queue = JobQueue(session, "scheduler")
        for p in projects:
            queue.enqueue(
                tenant_id=p.tenant_id,
                project_id=p.id,
                kind="analytics.daily",
                logical_key=f"daily:{p.id}:{local_date.isoformat()}",
                payload={"local_date": local_date.isoformat()},
            )
        print(f"enqueued daily metrics for {len(projects)} projects on {local_date}")
    return 0


def cmd_smoke(args: argparse.Namespace) -> int:
    from dataagent.storage.database import check_schema_compatible, init_engine, session_scope

    settings = get_settings()
    init_engine(settings.database_url)
    with session_scope() as session:
        ok = check_schema_compatible(session)
        print("schema_ok" if ok else "schema_missing")
        return 0 if ok else 1


def cmd_demo(args: argparse.Namespace) -> int:
    """Offline synthetic demo — seed tenants/agents, ingest fixtures, run experiment."""
    os.environ.setdefault("DATAAGENT_AUTH_MODE", "offline")
    os.environ.setdefault("DATAAGENT_AZURE_TRANSPORT", "fake")
    reset_settings()
    from dataagent.demo import run_demo

    run_demo()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dataagent")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("api")
    sub.add_parser("worker")
    sub.add_parser("migrate")
    p_daily = sub.add_parser("enqueue-daily")
    p_daily.add_argument("--date", default=None)
    sub.add_parser("smoke")
    sub.add_parser("demo")

    args = parser.parse_args(argv)
    handlers = {
        "api": cmd_api,
        "worker": cmd_worker,
        "migrate": cmd_migrate,
        "enqueue-daily": cmd_enqueue_daily,
        "smoke": cmd_smoke,
        "demo": cmd_demo,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
