"""Milestone 5 — auth matrix API checks, SDK install, many-agent load baseline."""

from __future__ import annotations

import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from dataagent.authorization.policy import bump_policy_revision, evaluate_effective_access
from dataagent.jobs.queue import JobQueue
from dataagent.storage import models as m
from dataagent.storage.models import DEFAULT_MODULES


def _headers(principal_subject: str) -> dict:
    return {"X-Dataagent-Principal": principal_subject}


@pytest.fixture
def api_world(engine):
    """Committed world for API TestClient (separate sessions)."""
    from dataagent.storage.database import session_scope
    from dataagent.api.app import seed_system_roles

    with session_scope() as session:
        seed_system_roles(session)
        ta = m.Tenant(slug=f"api-{uuid.uuid4().hex[:8]}", name="API Tenant")
        session.add(ta)
        session.flush()
        proj = m.Project(
            tenant_id=ta.id,
            slug="main",
            name="Main",
            modules=dict(DEFAULT_MODULES),
            timezone="UTC",
        )
        session.add(proj)
        session.flush()
        suffix = uuid.uuid4().hex[:8]
        admin = m.Principal(
            issuer="offline", subject=f"api-admin-{suffix}", display_name="Admin"
        )
        outsider = m.Principal(
            issuer="offline", subject=f"api-outsider-{suffix}", display_name="Out"
        )
        limited = m.Principal(
            issuer="offline", subject=f"api-limited-{suffix}", display_name="Lim"
        )
        session.add_all([admin, outsider, limited])
        session.flush()
        session.add(m.Membership(tenant_id=ta.id, project_id=proj.id, principal_id=admin.id))
        session.add(m.Membership(tenant_id=ta.id, project_id=proj.id, principal_id=limited.id))
        # outsider has no membership
        roles = {r.name: r for r in session.execute(select(m.Role)).scalars()}
        agents = []
        for slug in ("a1", "a2", "a3"):
            a = m.Agent(tenant_id=ta.id, project_id=proj.id, slug=slug, name=slug)
            session.add(a)
            agents.append(a)
        session.flush()
        session.add(
            m.ScopedRoleBinding(
                tenant_id=ta.id,
                project_id=proj.id,
                principal_id=admin.id,
                role_id=roles["project_admin"].id,
                scope_level="project",
                whole_project=True,
            )
        )
        session.add(
            m.ScopedRoleBinding(
                tenant_id=ta.id,
                project_id=proj.id,
                principal_id=limited.id,
                role_id=roles["analyst"].id,
                scope_level="project",
                agent_id=agents[0].id,
            )
        )
        session.add(m.PolicyCacheVersion(tenant_id=ta.id, project_id=proj.id, revision=1))
        src = m.Source(
            tenant_id=ta.id,
            project_id=proj.id,
            slug="src",
            name="Src",
            allowed_agent_ids=[str(agents[0].id)],
        )
        session.add(src)
        session.flush()
        ids = {
            "project_id": str(proj.id),
            "tenant_id": str(ta.id),
            "admin": admin.subject,
            "outsider": outsider.subject,
            "limited": limited.subject,
            "agent_ids": [str(a.id) for a in agents],
            "source_id": str(src.id),
        }
    return ids


def test_api_admin_can_create_agent(client: TestClient, api_world):
    pid = api_world["project_id"]
    r = client.post(
        f"/v1/projects/{pid}/agents",
        json={"slug": "new-one", "name": "New"},
        headers=_headers(api_world["admin"]),
    )
    assert r.status_code == 201, r.text


def test_api_outsider_denied(client: TestClient, api_world):
    pid = api_world["project_id"]
    r = client.get(
        f"/v1/projects/{pid}/agents",
        headers=_headers(api_world["outsider"]),
    )
    assert r.status_code == 403


def test_api_limited_cannot_register_agent(client: TestClient, api_world):
    pid = api_world["project_id"]
    r = client.post(
        f"/v1/projects/{pid}/agents",
        json={"slug": "nope", "name": "Nope"},
        headers=_headers(api_world["limited"]),
    )
    assert r.status_code == 403


def test_api_cross_project_id_swap(client: TestClient, api_world, engine):
    from dataagent.storage.database import session_scope

    with session_scope() as session:
        other = m.Project(
            tenant_id=uuid.UUID(api_world["tenant_id"]),
            slug="other",
            name="Other",
            modules=dict(DEFAULT_MODULES),
        )
        session.add(other)
        session.flush()
        other_id = str(other.id)
    r = client.post(
        f"/v1/projects/{other_id}/agents",
        json={"slug": "x", "name": "X"},
        headers=_headers(api_world["admin"]),
    )
    # admin not member of other project
    assert r.status_code == 403


def test_mismatched_source_agent_write(client: TestClient, api_world):
    pid = api_world["project_id"]
    # agent a2 not in source allowlist
    ev = {
        "events": [
            {
                "event_id": "bad-1",
                "event_type": "session.start",
                "schema_version": "1.0",
                "occurred_at": "2024-01-01T00:00:00Z",
                "agent_id": api_world["agent_ids"][1],
                "payload": {"session_id": "s"},
            }
        ]
    }
    r = client.post(
        f"/v1/projects/{pid}/events:batch",
        params={"source_id": api_world["source_id"]},
        json=ev,
        headers=_headers(api_world["admin"]),
    )
    assert r.status_code == 200
    assert r.json()["rejected"] == 1
    assert r.json()["accepted"] == 0


def test_revocation_bumps_policy(session, world):
    access1 = evaluate_effective_access(
        session,
        principal_id=world.analyst.id,
        tenant_id=world.tenant_a.id,
        project_id=world.project_a1.id,
    )
    assert access1.can("metrics.read", world.agents[0].id)
    binding = session.execute(
        select(m.ScopedRoleBinding).where(
            m.ScopedRoleBinding.principal_id == world.analyst.id,
            m.ScopedRoleBinding.status == "active",
        )
    ).scalars().first()
    binding.status = "revoked"
    binding.revoked_at = datetime.now(UTC)
    rev = bump_policy_revision(session, world.tenant_a.id, world.project_a1.id)
    access2 = evaluate_effective_access(
        session,
        principal_id=world.analyst.id,
        tenant_id=world.tenant_a.id,
        project_id=world.project_a1.id,
    )
    assert not access2.can("metrics.read", world.agents[0].id)
    assert access2.policy_revision == rev


def test_sdk_installs_without_server_deps():
    """SDK package is importable without pulling server dependencies into its modules."""
    import importlib
    from pathlib import Path

    # Confirm wheel/editable install metadata exists
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "show", "dataagent-sdk"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "Name: dataagent-sdk" in proc.stdout

    sdk = importlib.import_module("dataagent_sdk")
    assert not hasattr(sdk, "storage")
    pkg_dir = Path(sdk.__file__).parent
    for py in pkg_dir.glob("*.py"):
        src = py.read_text()
        assert "sqlalchemy" not in src
        assert "fastapi" not in src
        assert "alembic" not in src

    from dataagent_sdk import Client

    c = Client(base_url="http://example.invalid")
    tools = c.as_tools("00000000-0000-0000-0000-000000000000")
    assert any(t["name"] == "list_metrics" and not t["mutation"] for t in tools)
    assert any(t.get("mutation") for t in tools)


@pytest.mark.slow
def test_many_agent_load_baseline(session, world):
    """Configurable synthetic load — default 1000 agents across tenants."""
    import time
    from dataagent.config import get_settings

    n = get_settings().synthetic_agent_count
    # create second tenant project agents
    agents = []
    batch = []
    t0 = time.perf_counter()
    for i in range(n):
        tenant = world.tenant_a if i % 2 == 0 else world.tenant_b
        project = world.project_a1 if i % 2 == 0 else world.project_b1
        batch.append(
            m.Agent(
                tenant_id=tenant.id,
                project_id=project.id,
                slug=f"load-{i}",
                name=f"Load {i}",
            )
        )
        if len(batch) >= 200:
            session.add_all(batch)
            session.flush()
            agents.extend(batch)
            batch = []
    if batch:
        session.add_all(batch)
        session.flush()
        agents.extend(batch)
    insert_s = time.perf_counter() - t0

    t1 = time.perf_counter()
    rows = list(
        session.execute(
            select(m.Agent)
            .where(
                m.Agent.tenant_id == world.tenant_a.id,
                m.Agent.project_id == world.project_a1.id,
                m.Agent.status == "active",
            )
            .order_by(m.Agent.slug)
            .limit(100)
        ).scalars()
    )
    list_s = time.perf_counter() - t1
    assert len(rows) == 100

    # fair scheduling: enqueue jobs for both tenants and claim mixed
    q = JobQueue(session, "load-worker")
    for i in range(20):
        proj = world.project_a1 if i % 2 == 0 else world.project_b1
        tenant = proj.tenant_id
        q.enqueue(
            tenant_id=tenant,
            project_id=proj.id,
            kind="analytics.daily",
            logical_key=f"load-daily-{i}-{uuid.uuid4().hex[:6]}",
            payload={"local_date": "2024-01-01"},
            due_at=datetime.now(UTC) - timedelta(seconds=1),
        )
    claims = q.claim(limit=10)
    tenants_seen = {c.tenant_id for c in claims}
    # With SKIP LOCKED ordering by due_at, both tenants should get work when mixed
    assert len(claims) == 10
    print(
        f"LOAD_BASELINE agents={n} insert_s={insert_s:.3f} list100_s={list_s:.4f} "
        f"claim_tenants={len(tenants_seen)}"
    )
    assert insert_s < 120
    assert list_s < 2.0


def test_job_fencing(session, world):
    q1 = JobQueue(session, "worker-a", lease_seconds=60)
    job = q1.enqueue(
        tenant_id=world.tenant_a.id,
        project_id=world.project_a1.id,
        kind="analytics.daily",
        logical_key=f"fence-{uuid.uuid4().hex[:8]}",
        payload={"local_date": "2024-01-01"},
        due_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    claims = q1.claim(limit=1)
    assert claims and claims[0].job_id == job.id
    token = claims[0].fencing_token
    assert q1.write_checkpoint(job.id, token, {"step": 1})
    # stale worker with old token fails
    q2 = JobQueue(session, "worker-b")
    assert not q2.write_checkpoint(job.id, token - 1, {"step": 999})
    assert q1.complete(job.id, token, outcome="success")
