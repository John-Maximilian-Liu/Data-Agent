"""PostgreSQL-backed job queue — claim with SKIP LOCKED, fence checkpoints."""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from dataagent.storage import models as m


@dataclass
class JobClaim:
    job_id: uuid.UUID
    fencing_token: int
    kind: str
    payload: dict[str, Any]
    checkpoint: dict[str, Any]
    tenant_id: uuid.UUID
    project_id: uuid.UUID
    agent_id: uuid.UUID | None
    attempts: int
    initiating_principal_id: uuid.UUID | None
    policy_revision: int


class JobQueue:
    def __init__(self, session: Session, worker_id: str, lease_seconds: int = 60):
        self.session = session
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds

    def enqueue(
        self,
        *,
        tenant_id: uuid.UUID,
        project_id: uuid.UUID,
        kind: str,
        logical_key: str,
        payload: dict[str, Any] | None = None,
        agent_id: uuid.UUID | None = None,
        due_at: datetime | None = None,
        initiating_principal_id: uuid.UUID | None = None,
        policy_revision: int = 1,
        max_attempts: int = 8,
    ) -> m.Job:
        existing = self.session.execute(
            select(m.Job).where(
                m.Job.tenant_id == tenant_id,
                m.Job.project_id == project_id,
                m.Job.logical_key == logical_key,
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.status in ("queued", "running", "leased"):
                return existing
            # terminal — allow re-enqueue by appending suffix only if caller changes key
            return existing

        job = m.Job(
            tenant_id=tenant_id,
            project_id=project_id,
            agent_id=agent_id,
            kind=kind,
            logical_key=logical_key,
            status="queued",
            due_at=due_at or datetime.now(UTC),
            payload=payload or {},
            initiating_principal_id=initiating_principal_id,
            policy_revision=policy_revision,
            max_attempts=max_attempts,
        )
        self.session.add(job)
        self.session.flush()
        return job

    def claim(self, *, kinds: list[str] | None = None, limit: int = 1) -> list[JobClaim]:
        now = datetime.now(UTC)
        # Use SKIP LOCKED via raw SQL for claim race safety
        kind_filter = ""
        params: dict[str, Any] = {
            "now": now,
            "limit": limit,
            "owner": self.worker_id,
            "lease": now + timedelta(seconds=self.lease_seconds),
        }
        if kinds:
            kind_filter = "AND kind = ANY(:kinds)"
            params["kinds"] = kinds

        rows = self.session.execute(
            text(
                f"""
                SELECT id FROM job
                WHERE status IN ('queued', 'leased')
                  AND due_at <= :now
                  AND (lease_expires_at IS NULL OR lease_expires_at < :now)
                  AND attempts < max_attempts
                  {kind_filter}
                ORDER BY due_at
                FOR UPDATE SKIP LOCKED
                LIMIT :limit
                """
            ),
            params,
        ).fetchall()

        claims: list[JobClaim] = []
        for (job_id,) in rows:
            job = self.session.get(m.Job, job_id)
            if job is None:
                continue
            job.status = "leased"
            job.lease_owner = self.worker_id
            job.lease_expires_at = params["lease"]
            job.fencing_token += 1
            job.attempts += 1
            self.session.flush()
            claims.append(
                JobClaim(
                    job_id=job.id,
                    fencing_token=job.fencing_token,
                    kind=job.kind,
                    payload=dict(job.payload or {}),
                    checkpoint=dict(job.checkpoint or {}),
                    tenant_id=job.tenant_id,
                    project_id=job.project_id,
                    agent_id=job.agent_id,
                    attempts=job.attempts,
                    initiating_principal_id=job.initiating_principal_id,
                    policy_revision=job.policy_revision,
                )
            )
        return claims

    def write_checkpoint(
        self, job_id: uuid.UUID, fencing_token: int, data: dict[str, Any]
    ) -> bool:
        job = self.session.get(m.Job, job_id)
        if job is None or job.fencing_token != fencing_token:
            return False
        if job.lease_owner != self.worker_id:
            return False
        job.checkpoint = data
        self.session.add(
            m.Checkpoint(job_id=job_id, fencing_token=fencing_token, data=data)
        )
        self.session.flush()
        return True

    def complete(
        self,
        job_id: uuid.UUID,
        fencing_token: int,
        *,
        outcome: str = "success",
        error: str | None = None,
    ) -> bool:
        job = self.session.get(m.Job, job_id)
        if job is None or job.fencing_token != fencing_token:
            return False
        job.status = "completed" if outcome == "success" else "failed"
        if outcome == "canceled":
            job.status = "canceled"
        job.outcome = outcome
        job.error = error
        job.completed_at = datetime.now(UTC)
        job.lease_owner = None
        job.lease_expires_at = None
        self.session.flush()
        return True

    def retry_later(
        self, job_id: uuid.UUID, fencing_token: int, *, error: str | None = None
    ) -> bool:
        job = self.session.get(m.Job, job_id)
        if job is None or job.fencing_token != fencing_token:
            return False
        if job.attempts >= job.max_attempts:
            return self.complete(job_id, fencing_token, outcome="failed", error=error)
        # jittered backoff
        delay = min(3600, (2 ** min(job.attempts, 8)) + random.uniform(0, 2))
        job.status = "queued"
        job.due_at = datetime.now(UTC) + timedelta(seconds=delay)
        job.lease_owner = None
        job.lease_expires_at = None
        job.error = error
        self.session.flush()
        return True

    def cancel_pending_for_principal_scope(
        self,
        *,
        tenant_id: uuid.UUID,
        project_id: uuid.UUID,
        agent_ids_to_cancel: set[uuid.UUID],
    ) -> int:
        """On revoked scope, cancel unsafe pending work for those agents."""
        q = select(m.Job).where(
            m.Job.tenant_id == tenant_id,
            m.Job.project_id == project_id,
            m.Job.status.in_(("queued", "leased")),
            m.Job.agent_id.in_(list(agent_ids_to_cancel)),
        )
        count = 0
        for job in self.session.execute(q).scalars():
            job.status = "canceled"
            job.outcome = "scope_revoked"
            job.completed_at = datetime.now(UTC)
            count += 1
        self.session.flush()
        return count
