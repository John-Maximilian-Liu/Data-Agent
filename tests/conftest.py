"""PostgreSQL integration fixtures — two tenants, multi-agent auth matrix seed."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

# Force offline/fake before imports that cache settings
os.environ["DATAAGENT_AUTH_MODE"] = "offline"
os.environ["DATAAGENT_AZURE_TRANSPORT"] = "fake"
os.environ["DATAAGENT_ENVIRONMENT"] = "test"
os.environ.setdefault(
    "DATAAGENT_DATABASE_URL",
    "postgresql+psycopg://dataagent:dataagent@127.0.0.1:5432/dataagent_test",
)

from dataagent.config import reset_settings, get_settings
from dataagent.storage.database import init_engine
from dataagent.storage import models as m
from dataagent.storage.models import Base, DEFAULT_MODULES
from dataagent.api.app import seed_system_roles, create_app

reset_settings()


@pytest.fixture(scope="session")
def engine():
    settings = get_settings()
    eng = init_engine(settings.database_url)
    # recreate schema for test DB
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    Base.metadata.create_all(eng)
    # mark alembic version for readiness checks
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)"))
        conn.execute(text("DELETE FROM alembic_version"))
        conn.execute(text("INSERT INTO alembic_version(version_num) VALUES ('test')"))
    yield eng
    Base.metadata.drop_all(eng)


@pytest.fixture
def session(engine) -> Session:
    connection = engine.connect()
    transaction = connection.begin()
    sess = Session(bind=connection, expire_on_commit=False)
    seed_system_roles(sess)
    sess.flush()
    yield sess
    sess.close()
    transaction.rollback()
    connection.close()


@dataclass
class World:
    tenant_a: m.Tenant
    tenant_b: m.Tenant
    project_a1: m.Project
    project_a2: m.Project
    project_b1: m.Project
    agents: list[m.Agent]
    admin: m.Principal
    analyst: m.Principal
    developer: m.Principal
    roles: dict[str, m.Role]
    source: m.Source
    group: m.AgentGroup


@pytest.fixture
def world(session: Session) -> World:
    """Two tenants, two projects in tenant A, four agents, overlapping groups, dual-role principal."""
    ta = m.Tenant(slug=f"ta-{uuid.uuid4().hex[:8]}", name="Tenant A")
    tb = m.Tenant(slug=f"tb-{uuid.uuid4().hex[:8]}", name="Tenant B")
    session.add_all([ta, tb])
    session.flush()

    pa1 = m.Project(
        tenant_id=ta.id,
        slug="p1",
        name="Project A1",
        timezone="America/New_York",
        modules=dict(DEFAULT_MODULES),
    )
    pa2 = m.Project(
        tenant_id=ta.id,
        slug="p2",
        name="Project A2",
        timezone="UTC",
        modules=dict(DEFAULT_MODULES),
    )
    pb1 = m.Project(
        tenant_id=tb.id,
        slug="p1",  # duplicate slug across tenants OK
        name="Project B1",
        timezone="UTC",
        modules=dict(DEFAULT_MODULES),
    )
    session.add_all([pa1, pa2, pb1])
    session.flush()

    admin = m.Principal(issuer="offline", subject=f"admin-{uuid.uuid4().hex[:6]}", display_name="Admin")
    analyst = m.Principal(issuer="offline", subject=f"analyst-{uuid.uuid4().hex[:6]}", display_name="Analyst")
    developer = m.Principal(
        issuer="offline", subject=f"dev-{uuid.uuid4().hex[:6]}", display_name="Developer"
    )
    session.add_all([admin, analyst, developer])
    session.flush()

    for principal in (admin, analyst, developer):
        session.add(m.Membership(tenant_id=ta.id, project_id=pa1.id, principal_id=principal.id))
    session.add(m.Membership(tenant_id=ta.id, project_id=pa2.id, principal_id=admin.id))
    session.add(m.Membership(tenant_id=tb.id, project_id=pb1.id, principal_id=admin.id))

    roles = {r.name: r for r in session.execute(select(m.Role)).scalars()}
    agents = []
    for slug in ("alpha", "beta", "gamma", "delta"):
        # duplicate external-facing slug pattern across projects via separate agents
        a = m.Agent(tenant_id=ta.id, project_id=pa1.id, slug=slug, name=slug.title())
        session.add(a)
        agents.append(a)
    # agent in other project with same slug
    other = m.Agent(tenant_id=ta.id, project_id=pa2.id, slug="alpha", name="Alpha P2")
    session.add(other)
    session.flush()

    g1 = m.AgentGroup(tenant_id=ta.id, project_id=pa1.id, slug="g-ab", name="AB")
    g2 = m.AgentGroup(tenant_id=ta.id, project_id=pa1.id, slug="g-bc", name="BC")
    session.add_all([g1, g2])
    session.flush()
    for aid in (agents[0].id, agents[1].id):
        session.add(
            m.AgentGroupMembership(
                tenant_id=ta.id, project_id=pa1.id, group_id=g1.id, agent_id=aid
            )
        )
    for aid in (agents[1].id, agents[2].id):
        session.add(
            m.AgentGroupMembership(
                tenant_id=ta.id, project_id=pa1.id, group_id=g2.id, agent_id=aid
            )
        )

    # Admin whole project
    session.add(
        m.ScopedRoleBinding(
            tenant_id=ta.id,
            project_id=pa1.id,
            principal_id=admin.id,
            role_id=roles["project_admin"].id,
            scope_level="project",
            whole_project=True,
        )
    )
    # Developer: different roles on two agents (metrics on alpha, developer on beta)
    session.add(
        m.ScopedRoleBinding(
            tenant_id=ta.id,
            project_id=pa1.id,
            principal_id=developer.id,
            role_id=roles["analyst"].id,
            scope_level="project",
            agent_id=agents[0].id,
        )
    )
    session.add(
        m.ScopedRoleBinding(
            tenant_id=ta.id,
            project_id=pa1.id,
            principal_id=developer.id,
            role_id=roles["agent_developer"].id,
            scope_level="project",
            agent_id=agents[1].id,
        )
    )
    # Analyst: aggregates only on group AB (alpha+beta)
    session.add(
        m.ScopedRoleBinding(
            tenant_id=ta.id,
            project_id=pa1.id,
            principal_id=analyst.id,
            role_id=roles["analyst"].id,
            scope_level="project",
            agent_group_id=g1.id,
        )
    )
    session.add(m.PolicyCacheVersion(tenant_id=ta.id, project_id=pa1.id, revision=1))

    src = m.Source(
        tenant_id=ta.id,
        project_id=pa1.id,
        slug="main",
        name="Main",
        allowed_agent_ids=[str(a.id) for a in agents],
    )
    session.add(src)
    session.flush()

    return World(
        tenant_a=ta,
        tenant_b=tb,
        project_a1=pa1,
        project_a2=pa2,
        project_b1=pb1,
        agents=agents,
        admin=admin,
        analyst=analyst,
        developer=developer,
        roles=roles,
        source=src,
        group=g1,
    )


@pytest.fixture
def app(engine):
    return create_app()


@pytest.fixture
def client(app, engine):
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c
