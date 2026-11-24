"""System role definitions — actions and sensitive fields."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RoleDef:
    name: str
    actions: frozenset[str]
    fields: frozenset[str]
    delegable: bool = False
    description: str = ""


# Action vocabulary
ACTIONS = frozenset(
    {
        "tenant.admin",
        "project.admin",
        "agent.register",
        "agent.update",
        "agent.pause",
        "agent.archive",
        "source.register",
        "source.write",
        "mapping.publish",
        "metrics.read",
        "traces.read",
        "feedback.read",
        "cost.read",
        "export",
        "benchmark.create",
        "benchmark.import",
        "benchmark.refresh",
        "benchmark.review",
        "benchmark.publish",
        "experiment.create",
        "experiment.run",
        "experiment.cancel",
        "experiment.read",
        "report.read",
        "evidence.read",
        "role.bind",
        "role.revoke",
        "membership.manage",
        "credential.manage",
        "settings.update",
        "ci.submit",
        "job.read",
    }
)

FIELDS = frozenset(
    {
        "aggregates",
        "cost",
        "raw_text",
        "traces",
        "evidence",
        "holdout",
        "judge_detail",
        "exports",
    }
)

SYSTEM_ROLES: dict[str, RoleDef] = {
    "tenant_admin": RoleDef(
        name="tenant_admin",
        actions=frozenset(
            {
                "tenant.admin",
                "project.admin",
                "role.bind",
                "role.revoke",
                "membership.manage",
            }
        ),
        fields=frozenset({"aggregates"}),
        delegable=True,
        description="Create/administer projects; data permissions are separate.",
    ),
    "project_admin": RoleDef(
        name="project_admin",
        actions=frozenset(
            {
                "project.admin",
                "agent.register",
                "agent.update",
                "agent.pause",
                "agent.archive",
                "source.register",
                "source.write",
                "mapping.publish",
                "metrics.read",
                "benchmark.create",
                "benchmark.import",
                "benchmark.refresh",
                "benchmark.publish",
                "experiment.create",
                "experiment.run",
                "experiment.cancel",
                "experiment.read",
                "report.read",
                "role.bind",
                "role.revoke",
                "membership.manage",
                "credential.manage",
                "settings.update",
                "job.read",
            }
        ),
        fields=frozenset({"aggregates", "traces"}),
        delegable=True,
        description="Register agents/sources, modules, groups, refresh, delegate.",
    ),
    "agent_owner": RoleDef(
        name="agent_owner",
        actions=frozenset(
            {
                "agent.update",
                "agent.pause",
                "metrics.read",
                "traces.read",
                "experiment.create",
                "experiment.run",
                "experiment.read",
                "report.read",
                "job.read",
            }
        ),
        fields=frozenset({"aggregates", "traces"}),
        delegable=False,
        description="Manage catalog/config and test releases for allocated agents.",
    ),
    "agent_developer": RoleDef(
        name="agent_developer",
        actions=frozenset(
            {
                "metrics.read",
                "traces.read",
                "experiment.create",
                "experiment.run",
                "experiment.read",
                "report.read",
                "job.read",
            }
        ),
        fields=frozenset({"aggregates", "traces"}),
        delegable=False,
    ),
    "benchmark_reviewer": RoleDef(
        name="benchmark_reviewer",
        actions=frozenset({"benchmark.review", "benchmark.create", "metrics.read"}),
        fields=frozenset({"aggregates", "evidence"}),
        delegable=False,
    ),
    "analyst": RoleDef(
        name="analyst",
        actions=frozenset({"metrics.read", "job.read"}),
        fields=frozenset({"aggregates"}),
        delegable=False,
    ),
    "viewer": RoleDef(
        name="viewer",
        actions=frozenset({"metrics.read"}),
        fields=frozenset({"aggregates"}),
        delegable=False,
    ),
    "service_account": RoleDef(
        name="service_account",
        actions=frozenset({"source.write", "ci.submit", "job.read"}),
        fields=frozenset(),
        delegable=False,
    ),
}


def get_system_role(name: str) -> RoleDef:
    if name not in SYSTEM_ROLES:
        raise KeyError(f"unknown system role: {name}")
    return SYSTEM_ROLES[name]


def role_payload(role: RoleDef) -> dict[str, Any]:
    return {
        "name": role.name,
        "actions": sorted(role.actions),
        "fields": sorted(role.fields),
        "delegable": role.delegable,
        "description": role.description,
    }
