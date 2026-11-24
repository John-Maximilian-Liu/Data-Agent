"""Scoped authorization — default deny, explicit bindings, no cross-agent field mashup."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from dataagent.authorization.roles import SYSTEM_ROLES, RoleDef
from dataagent.storage import models as m


@dataclass
class AuthContext:
    principal_id: uuid.UUID
    tenant_id: uuid.UUID
    project_id: uuid.UUID | None
    display_name: str
    kind: str = "user"
    request_id: str = ""
    auth_mode: str = "offline"
    # Service credential context
    credential_id: uuid.UUID | None = None
    allowed_agent_ids: frozenset[uuid.UUID] = field(default_factory=frozenset)
    scopes: frozenset[str] = field(default_factory=frozenset)
    single_agent_id: uuid.UUID | None = None
    policy_revision: int = 1


@dataclass
class AgentEntitlement:
    """Entitlements justified by one or more full applicable bindings for this agent."""

    agent_id: uuid.UUID
    actions: set[str] = field(default_factory=set)
    fields: set[str] = field(default_factory=set)
    binding_ids: set[uuid.UUID] = field(default_factory=set)
    whole_project: bool = False


@dataclass
class EffectiveAccess:
    tenant_id: uuid.UUID
    project_id: uuid.UUID | None
    principal_id: uuid.UUID
    policy_revision: int
    # agent_id -> entitlement (only agents with at least one binding)
    by_agent: dict[uuid.UUID, AgentEntitlement] = field(default_factory=dict)
    # whole-project entitlements (require explicit whole_project binding)
    project_actions: set[str] = field(default_factory=set)
    project_fields: set[str] = field(default_factory=set)
    tenant_actions: set[str] = field(default_factory=set)
    modules: dict[str, bool] = field(default_factory=dict)

    def authorized_agent_ids(self, action: str | None = None) -> set[uuid.UUID]:
        if action is None:
            return set(self.by_agent.keys())
        return {aid for aid, ent in self.by_agent.items() if action in ent.actions}

    def can(self, action: str, agent_id: uuid.UUID | None = None) -> bool:
        if action in self.tenant_actions:
            return True
        if agent_id is None:
            return action in self.project_actions or any(
                action in e.actions for e in self.by_agent.values()
            )
        ent = self.by_agent.get(agent_id)
        if ent and action in ent.actions:
            return True
        # whole-project binding covers agents only for actions granted project-wide
        return action in self.project_actions

    def can_field(self, field_name: str, agent_id: uuid.UUID) -> bool:
        """Field access must come from a binding that also covers this agent."""
        ent = self.by_agent.get(agent_id)
        if ent and field_name in ent.fields:
            return True
        # project-wide field grant (e.g. billing) requires explicit whole_project binding
        return field_name in self.project_fields and agent_id in self.by_agent

    def can_action_and_field(self, action: str, field_name: str, agent_id: uuid.UUID) -> bool:
        """Both must be obtained within an applicable binding for the same resource."""
        ent = self.by_agent.get(agent_id)
        if ent and action in ent.actions and field_name in ent.fields:
            return True
        if (
            action in self.project_actions
            and field_name in self.project_fields
            and agent_id in self.by_agent
        ):
            return True
        return False


def _role_def_from_row(role: m.Role) -> RoleDef:
    if role.is_system and role.name in SYSTEM_ROLES:
        return SYSTEM_ROLES[role.name]
    return RoleDef(
        name=role.name,
        actions=frozenset(role.actions or []),
        fields=frozenset(role.fields or []),
        delegable=role.delegable,
    )


def bump_policy_revision(session: Session, tenant_id: uuid.UUID, project_id: uuid.UUID) -> int:
    row = session.execute(
        select(m.PolicyCacheVersion).where(
            m.PolicyCacheVersion.tenant_id == tenant_id,
            m.PolicyCacheVersion.project_id == project_id,
        )
    ).scalar_one_or_none()
    if row is None:
        row = m.PolicyCacheVersion(tenant_id=tenant_id, project_id=project_id, revision=1)
        session.add(row)
        session.flush()
        return 1
    row.revision += 1
    session.flush()
    return row.revision


def _group_agent_ids(
    session: Session, tenant_id: uuid.UUID, project_id: uuid.UUID, group_id: uuid.UUID
) -> set[uuid.UUID]:
    rows = session.execute(
        select(m.AgentGroupMembership.agent_id).where(
            m.AgentGroupMembership.tenant_id == tenant_id,
            m.AgentGroupMembership.project_id == project_id,
            m.AgentGroupMembership.group_id == group_id,
        )
    ).scalars()
    return set(rows)


def evaluate_effective_access(
    session: Session,
    *,
    principal_id: uuid.UUID,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID | None,
    now: datetime | None = None,
) -> EffectiveAccess:
    now = now or datetime.now(UTC)
    access = EffectiveAccess(
        tenant_id=tenant_id,
        project_id=project_id,
        principal_id=principal_id,
        policy_revision=1,
    )

    # membership required
    membership_q = select(m.Membership).where(
        m.Membership.tenant_id == tenant_id,
        m.Membership.principal_id == principal_id,
        m.Membership.status == "active",
    )
    memberships = list(session.execute(membership_q).scalars())
    if not memberships:
        return access

    if project_id is not None:
        proj = session.get(m.Project, project_id)
        if proj is None or proj.tenant_id != tenant_id:
            return access
        access.modules = {k: bool(v) for k, v in (proj.modules or {}).items()}
        pol = session.execute(
            select(m.PolicyCacheVersion).where(
                m.PolicyCacheVersion.tenant_id == tenant_id,
                m.PolicyCacheVersion.project_id == project_id,
            )
        ).scalar_one_or_none()
        if pol:
            access.policy_revision = pol.revision

    bindings = list(
        session.execute(
            select(m.ScopedRoleBinding).where(
                m.ScopedRoleBinding.tenant_id == tenant_id,
                m.ScopedRoleBinding.principal_id == principal_id,
                m.ScopedRoleBinding.status == "active",
            )
        ).scalars()
    )

    for binding in bindings:
        if binding.expires_at and binding.expires_at < now:
            continue
        if binding.scope_level == "tenant":
            role = session.get(m.Role, binding.role_id)
            if role is None:
                continue
            rd = _role_def_from_row(role)
            access.tenant_actions |= set(rd.actions)
            continue

        if project_id is None or binding.project_id != project_id:
            continue

        role = session.get(m.Role, binding.role_id)
        if role is None:
            continue
        rd = _role_def_from_row(role)

        agent_ids: set[uuid.UUID] = set()
        whole = False
        if binding.whole_project:
            whole = True
            # project-wide: grant project_actions/fields; also cover all project agents
            access.project_actions |= set(rd.actions)
            access.project_fields |= set(rd.fields)
            agent_ids = set(
                session.execute(
                    select(m.Agent.id).where(
                        m.Agent.tenant_id == tenant_id,
                        m.Agent.project_id == project_id,
                        m.Agent.status != "archived",
                    )
                ).scalars()
            )
        elif binding.agent_id is not None:
            agent_ids = {binding.agent_id}
        elif binding.agent_group_id is not None:
            agent_ids = _group_agent_ids(session, tenant_id, project_id, binding.agent_group_id)

        for aid in agent_ids:
            ent = access.by_agent.get(aid)
            if ent is None:
                ent = AgentEntitlement(agent_id=aid, whole_project=whole)
                access.by_agent[aid] = ent
            # Each entitlement justified by a full applicable binding — union within agent
            ent.actions |= set(rd.actions)
            ent.fields |= set(rd.fields)
            ent.binding_ids.add(binding.id)
            if whole:
                ent.whole_project = True

    return access


class AuthorizationError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def authorize(
    access: EffectiveAccess,
    action: str,
    *,
    agent_id: uuid.UUID | None = None,
    field_name: str | None = None,
    module: str | None = None,
) -> None:
    if module and not access.modules.get(module, True):
        raise AuthorizationError("module_disabled", f"module {module} is disabled")
    if field_name and agent_id is not None:
        if not access.can_action_and_field(action, field_name, agent_id):
            raise AuthorizationError("forbidden", f"missing {action}+{field_name} on agent")
        return
    if not access.can(action, agent_id):
        raise AuthorizationError("forbidden", f"missing action {action}")


def require_action(access: EffectiveAccess, action: str, agent_id: uuid.UUID | None = None) -> None:
    authorize(access, action, agent_id=agent_id)


def require_fields(
    access: EffectiveAccess, action: str, field_name: str, agent_ids: set[uuid.UUID]
) -> set[uuid.UUID]:
    """Return agent IDs for which both action and field are granted; never mash scopes."""
    allowed: set[uuid.UUID] = set()
    for aid in agent_ids:
        if access.can_action_and_field(action, field_name, aid):
            allowed.add(aid)
    return allowed


def filter_authorized_agents(
    access: EffectiveAccess, requested: set[uuid.UUID] | None, action: str = "metrics.read"
) -> set[uuid.UUID]:
    authorized = access.authorized_agent_ids(action)
    if requested is None:
        return authorized
    return authorized & requested
