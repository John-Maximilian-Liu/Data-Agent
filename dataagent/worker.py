"""Background worker — claim jobs with SKIP LOCKED and execute handlers."""

from __future__ import annotations

import logging
import time
import uuid

from dataagent.benchmarks.service import refresh_benchmark
from dataagent.config import get_settings
from dataagent.experiments.runner import run_experiment
from dataagent.jobs.queue import JobQueue
from dataagent.analytics.metrics import compute_daily_metrics
from dataagent.storage.database import init_engine, session_scope
from dataagent.storage import models as m
from sqlalchemy import select
from datetime import date

log = logging.getLogger("dataagent.worker")


def process_once(worker_id: str | None = None) -> int:
    settings = get_settings()
    worker_id = worker_id or settings.worker_id
    processed = 0
    with session_scope() as session:
        queue = JobQueue(session, worker_id, lease_seconds=settings.job_lease_seconds)
        claims = queue.claim(limit=5)
        for claim in claims:
            log.info(
                "claimed job",
                extra={
                    "job_id": str(claim.job_id),
                    "kind": claim.kind,
                    "project_id": str(claim.project_id),
                    "fencing_token": claim.fencing_token,
                },
            )
            try:
                _handle(session, queue, claim)
                queue.complete(claim.job_id, claim.fencing_token, outcome="success")
                processed += 1
            except Exception as e:
                log.exception("job failed")
                queue.retry_later(claim.job_id, claim.fencing_token, error=str(e)[:500])
    return processed


def _handle(session, queue: JobQueue, claim) -> None:
    kind = claim.kind
    payload = claim.payload
    if kind == "experiment.run":
        # recheck eligibility — policy may have changed
        exp_id = uuid.UUID(payload["experiment_id"])
        run_experiment(session, exp_id)
        return
    if kind == "benchmark.refresh":
        bid = uuid.UUID(payload["benchmark_id"])
        agent_ids = (
            {uuid.UUID(x) for x in payload["agent_ids"]} if payload.get("agent_ids") else None
        )
        refresh_benchmark(
            session,
            tenant_id=claim.tenant_id,
            project_id=claim.project_id,
            benchmark_id=bid,
            actor_id=claim.initiating_principal_id,
            participating_agent_ids=agent_ids,
        )
        return
    if kind == "analytics.daily":
        local_date = date.fromisoformat(payload["local_date"])
        agent_ids = {
            a.id
            for a in session.execute(
                select(m.Agent).where(
                    m.Agent.tenant_id == claim.tenant_id,
                    m.Agent.project_id == claim.project_id,
                    m.Agent.status == "active",
                )
            ).scalars()
        }
        compute_daily_metrics(
            session,
            tenant_id=claim.tenant_id,
            project_id=claim.project_id,
            agent_ids=agent_ids,
            local_date=local_date,
        )
        return
    raise ValueError(f"unknown job kind {kind}")


def run_forever() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    init_engine(settings.database_url)
    log.info("worker starting", extra={"worker_id": settings.worker_id})
    while True:
        n = process_once()
        if n == 0:
            time.sleep(1.0)
