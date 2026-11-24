"""Milestone 1 — catalog isolation and authorization matrix."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from dataagent.authorization.policy import evaluate_effective_access
from dataagent.storage import models as m


def test_duplicate_slugs_across_tenants(session, world):
    assert world.project_a1.slug == world.project_b1.slug
    assert world.project_a1.tenant_id != world.project_b1.tenant_id


def test_agent_scoped_to_project(session, world):
    alphas = list(
        session.execute(
            select(m.Agent).where(m.Agent.slug == "alpha", m.Agent.tenant_id == world.tenant_a.id)
        ).scalars()
    )
    assert len(alphas) == 2
    projects = {a.project_id for a in alphas}
    assert world.project_a1.id in projects and world.project_a2.id in projects


def test_developer_different_roles_on_two_agents(session, world):
    access = evaluate_effective_access(
        session,
        principal_id=world.developer.id,
        tenant_id=world.tenant_a.id,
        project_id=world.project_a1.id,
    )
    alpha, beta, gamma = world.agents[0], world.agents[1], world.agents[2]
    assert access.can("metrics.read", alpha.id)
    assert not access.can("experiment.create", alpha.id)
    assert access.can("experiment.create", beta.id)
    assert not access.can("metrics.read", gamma.id)


def test_analyst_group_scope(session, world):
    access = evaluate_effective_access(
        session,
        principal_id=world.analyst.id,
        tenant_id=world.tenant_a.id,
        project_id=world.project_a1.id,
    )
    assert world.agents[0].id in access.by_agent
    assert world.agents[1].id in access.by_agent
    assert world.agents[2].id not in access.by_agent
    assert not access.can_field("cost", world.agents[0].id)


def test_no_cross_agent_field_mashup(session, world):
    """Cost role on A + trace role on B must not reveal B's costs."""
    # grant developer cost field only on alpha via custom role
    custom = m.Role(
        tenant_id=world.tenant_a.id,
        name=f"cost_only_{uuid.uuid4().hex[:6]}",
        actions=["metrics.read"],
        fields=["cost", "aggregates"],
    )
    session.add(custom)
    session.flush()
    session.add(
        m.ScopedRoleBinding(
            tenant_id=world.tenant_a.id,
            project_id=world.project_a1.id,
            principal_id=world.developer.id,
            role_id=custom.id,
            scope_level="project",
            agent_id=world.agents[0].id,
        )
    )
    access = evaluate_effective_access(
        session,
        principal_id=world.developer.id,
        tenant_id=world.tenant_a.id,
        project_id=world.project_a1.id,
    )
    assert access.can_action_and_field("metrics.read", "cost", world.agents[0].id)
    # beta has traces via agent_developer but not cost
    assert access.can("traces.read", world.agents[1].id)
    assert not access.can_field("cost", world.agents[1].id)


def test_admin_whole_project(session, world):
    access = evaluate_effective_access(
        session,
        principal_id=world.admin.id,
        tenant_id=world.tenant_a.id,
        project_id=world.project_a1.id,
    )
    assert "project.admin" in access.project_actions
    for a in world.agents:
        assert access.can("agent.register") or access.can("agent.update", a.id)
