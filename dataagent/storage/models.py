"""SQLAlchemy models with tenant/project (and agent) scope constraints."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


DEFAULT_MODULES = {
    "sessions": True,
    "feedback": True,
    "cost": True,
    "activity": True,
    "kb": True,
    "tools": True,
    "skills": True,
    "benchmark": True,
    "experiments": True,
    "reports": True,
}


# ---------------------------------------------------------------------------
# Identity & catalog
# ---------------------------------------------------------------------------


class Tenant(Base):
    __tablename__ = "tenant"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    slug: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    modules: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    ingest_limit_rpm: Mapped[int] = mapped_column(Integer, default=10_000)
    eval_concurrency: Mapped[int] = mapped_column(Integer, default=32)
    cost_budget_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)


class Project(Base):
    __tablename__ = "project"
    __table_args__ = (
        UniqueConstraint("tenant_id", "slug", name="uq_project_tenant_slug"),
        UniqueConstraint("tenant_id", "id", name="uq_project_tenant_id"),
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"], name="fk_project_tenant"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    modules: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        server_default=text("'{}'::jsonb"),
    )
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))


class Principal(Base):
    __tablename__ = "principal"
    __table_args__ = (UniqueConstraint("issuer", "subject", name="uq_principal_issuer_subject"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    issuer: Mapped[str] = mapped_column(String(512), nullable=False)
    subject: Mapped[str] = mapped_column(String(512), nullable=False)
    display_name: Mapped[str] = mapped_column(String(256), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), default="user")  # user | service
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Membership(Base):
    """Active product membership binding a principal to a tenant (and optional project)."""

    __tablename__ = "membership"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "principal_id", "project_id", name="uq_membership_scope_principal"
        ),
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        ForeignKeyConstraint(["project_id"], ["project.id"]),
        ForeignKeyConstraint(["principal_id"], ["principal.id"]),
        Index("ix_membership_principal", "principal_id", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    principal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Role(Base):
    __tablename__ = "role"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_role_tenant_name"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    actions: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    fields: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    delegable: Mapped[bool] = mapped_column(Boolean, default=False)


class Agent(Base):
    __tablename__ = "agent"
    __table_args__ = (
        UniqueConstraint("tenant_id", "project_id", "slug", name="uq_agent_scope_slug"),
        UniqueConstraint("tenant_id", "project_id", "id", name="uq_agent_scope_id"),
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["project.tenant_id", "project.id"],
            name="fk_agent_project_scope",
        ),
        CheckConstraint("status IN ('active','paused','archived')", name="agent_status"),
        Index("ix_agent_project_status", "tenant_id", "project_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    slug: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active")
    owner_team: Mapped[str | None] = mapped_column(String(256), nullable=True)
    owner_principal_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AgentGroup(Base):
    __tablename__ = "agent_group"
    __table_args__ = (
        UniqueConstraint("tenant_id", "project_id", "slug", name="uq_agent_group_slug"),
        UniqueConstraint("tenant_id", "project_id", "id", name="uq_agent_group_scope_id"),
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["project.tenant_id", "project.id"],
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    slug: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgentGroupMembership(Base):
    __tablename__ = "agent_group_membership"
    __table_args__ = (
        PrimaryKeyConstraint("group_id", "agent_id"),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "group_id"],
            ["agent_group.tenant_id", "agent_group.project_id", "agent_group.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "agent_id"],
            ["agent.tenant_id", "agent.project_id", "agent.id"],
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    group_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Release(Base):
    __tablename__ = "release"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "project_id", "agent_id", "version", name="uq_release_version"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "agent_id"],
            ["agent.tenant_id", "agent.project_id", "agent.id"],
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    version: Mapped[str] = mapped_column(String(128), nullable=False)
    endpoint_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    ci_target: Mapped[str | None] = mapped_column(String(512), nullable=True)
    artifact_versions: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    is_production: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ScopedRoleBinding(Base):
    __tablename__ = "scoped_role_binding"
    __table_args__ = (
        CheckConstraint(
            "(scope_level = 'tenant' AND project_id IS NULL AND agent_id IS NULL "
            "AND agent_group_id IS NULL AND whole_project = false) OR "
            "(scope_level = 'project' AND project_id IS NOT NULL AND ("
            "(agent_id IS NOT NULL AND agent_group_id IS NULL AND whole_project = false) OR "
            "(agent_id IS NULL AND agent_group_id IS NOT NULL AND whole_project = false) OR "
            "(agent_id IS NULL AND agent_group_id IS NULL AND whole_project = true)"
            "))",
            name="binding_selector",
        ),
        Index(
            "ix_binding_lookup",
            "tenant_id",
            "project_id",
            "principal_id",
            "status",
        ),
        ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        ForeignKeyConstraint(["principal_id"], ["principal.id"]),
        ForeignKeyConstraint(["role_id"], ["role.id"]),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    principal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    role_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    scope_level: Mapped[str] = mapped_column(String(32), nullable=False)  # tenant | project
    agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    agent_group_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    whole_project: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32), default="active")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    grantor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ServiceCredential(Base):
    __tablename__ = "service_credential"
    __table_args__ = (
        UniqueConstraint("tenant_id", "project_id", "name", name="uq_credential_name"),
        Index("ix_credential_hash", "secret_hash"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)  # source | agent | ci
    secret_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    source_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    allowed_agent_ids: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    scopes: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    status: Mapped[str] = mapped_column(String(32), default="active")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rotation_overlap_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Source(Base):
    __tablename__ = "source"
    __table_args__ = (
        UniqueConstraint("tenant_id", "project_id", "slug", name="uq_source_slug"),
        UniqueConstraint("tenant_id", "project_id", "id", name="uq_source_scope_id"),
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["project.tenant_id", "project.id"],
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    slug: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), default="native")  # native | otlp | webhook
    capabilities: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    allowed_agent_ids: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    single_agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MappingRevision(Base):
    __tablename__ = "mapping_revision"
    __table_args__ = (
        UniqueConstraint("source_id", "revision", name="uq_mapping_revision"),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "source_id"],
            ["source.tenant_id", "source.project_id", "source.id"],
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="draft")  # draft | published
    mapping: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ---------------------------------------------------------------------------
# Observation
# ---------------------------------------------------------------------------


class Receipt(Base):
    __tablename__ = "receipt"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "project_id", "source_id", "external_event_id", name="uq_receipt_external"
        ),
        Index("ix_receipt_hash", "tenant_id", "project_id", "content_hash"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    external_event_id: Mapped[str] = mapped_column(String(256), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    # accepted | duplicate | rejected | content_conflict
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    diagnostic_code: Mapped[str | None] = mapped_column(String(64), nullable=True)


class Session(Base):
    __tablename__ = "session"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "project_id", "agent_id", "external_session_id", name="uq_session_ext"
        ),
        Index("ix_session_day", "tenant_id", "project_id", "agent_id", "local_date"),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "agent_id"],
            ["agent.tenant_id", "agent.project_id", "agent.id"],
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    release_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    external_session_id: Mapped[str] = mapped_column(String(256), nullable=False)
    user_pseudonym: Mapped[str | None] = mapped_column(String(128), nullable=True)
    state: Mapped[str] = mapped_column(String(32), default="started")
    # started | completed | abandoned | timed_out | unknown
    traffic_tag: Mapped[str] = mapped_column(String(32), default="production")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    local_date: Mapped[date] = mapped_column(Date, nullable=False)
    technical_completed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    business_success: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, server_default=text("'{}'::jsonb")
    )


class Run(Base):
    __tablename__ = "run"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "project_id", "agent_id", "external_run_id", name="uq_run_ext"
        ),
        Index("ix_run_session", "session_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    external_run_id: Mapped[str] = mapped_column(String(256), nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="completed")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    wall_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    release_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    unresolved_parent: Mapped[bool] = mapped_column(Boolean, default=False)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, server_default=text("'{}'::jsonb")
    )


class Span(Base):
    __tablename__ = "span"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "project_id", "trace_id", "span_id", name="uq_span_identity"
        ),
        Index("ix_span_parent", "trace_id", "parent_span_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    span_id: Mapped[str] = mapped_column(String(32), nullable=False)
    parent_span_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), default="internal")
    # model | retrieval | tool | skill | internal
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="ok")
    component_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    unresolved_parent: Mapped[bool] = mapped_column(Boolean, default=False)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    mapper_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)


class FeedbackRevision(Base):
    __tablename__ = "feedback_revision"
    __table_args__ = (
        Index(
            "ix_feedback_subject",
            "tenant_id",
            "project_id",
            "agent_id",
            "subject_type",
            "subject_id",
            "rater_pseudonym",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(32), nullable=False)  # session | run
    subject_id: Mapped[str] = mapped_column(String(256), nullable=False)
    rater_pseudonym: Mapped[str] = mapped_column(String(128), nullable=False)
    scale: Mapped[str] = mapped_column(String(64), nullable=False)
    score: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    retracted: Mapped[bool] = mapped_column(Boolean, default=False)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    comment_redacted: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, server_default=text("'{}'::jsonb")
    )


class Activity(Base):
    __tablename__ = "activity"
    __table_args__ = (
        Index("ix_activity_component", "tenant_id", "project_id", "agent_id", "kind", "component_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # kb | tool | skill | prompt
    component_id: Mapped[str] = mapped_column(String(256), nullable=False)
    version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    local_date: Mapped[date] = mapped_column(Date, nullable=False)
    success: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    is_citation: Mapped[bool] = mapped_column(Boolean, default=False)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))


class ModelUsage(Base):
    __tablename__ = "model_usage"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "provider_call_id",
            name="uq_model_usage_call",
        ),
        Index("ix_model_usage_day", "tenant_id", "project_id", "agent_id", "local_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    provider_call_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    deployment: Mapped[str | None] = mapped_column(String(128), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    local_date: Mapped[date] = mapped_column(Date, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_tokens: Mapped[int] = mapped_column(Integer, default=0)
    usage_class: Mapped[str] = mapped_column(String(32), default="production")
    # production | evaluation | judge
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    cost_estimated: Mapped[bool] = mapped_column(Boolean, default=False)
    rate_unknown: Mapped[bool] = mapped_column(Boolean, default=False)


class PriceSchedule(Base):
    __tablename__ = "price_schedule"
    __table_args__ = (
        UniqueConstraint(
            "model", "deployment", "effective_from", name="uq_price_schedule_effective"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    deployment: Mapped[str | None] = mapped_column(String(128), nullable=True)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    input_per_1k: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    output_per_1k: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    cache_per_1k: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal("0"))
    currency: Mapped[str] = mapped_column(String(8), default="USD")


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


class Benchmark(Base):
    __tablename__ = "benchmark"
    __table_args__ = (
        UniqueConstraint("tenant_id", "project_id", "slug", name="uq_benchmark_slug"),
        CheckConstraint(
            "ownership IN ('agent_scoped','project_shared')", name="benchmark_ownership"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    slug: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    ownership: Mapped[str] = mapped_column(String(32), default="agent_scoped")
    owner_agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CaseRevision(Base):
    __tablename__ = "case_revision"
    __table_args__ = (
        UniqueConstraint("benchmark_id", "external_case_id", "revision", name="uq_case_rev"),
        Index("ix_case_benchmark", "tenant_id", "project_id", "benchmark_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    benchmark_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    external_case_id: Mapped[str] = mapped_column(String(256), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    expected_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    multi_turn: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    language: Mapped[str | None] = mapped_column(String(32), nullable=True)
    task_category: Mapped[str | None] = mapped_column(String(128), nullable=True)
    rubric: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    document_labels: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    permitted_tools: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    permitted_skills: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    fixtures: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    source_agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    evidence_refs: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    provenance: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    sample_weight: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("1"))
    metric_eligibility: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    holdout: Mapped[bool] = mapped_column(Boolean, default=False)
    is_seed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BenchmarkVersion(Base):
    __tablename__ = "benchmark_version"
    __table_args__ = (
        UniqueConstraint("benchmark_id", "version", name="uq_benchmark_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    benchmark_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    artifact_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    published_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    watermark: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class SuiteMembership(Base):
    __tablename__ = "suite_membership"
    __table_args__ = (
        PrimaryKeyConstraint("benchmark_version_id", "case_revision_id", "suite"),
        CheckConstraint(
            "suite IN ('fixed_core','rolling_production','regression')", name="suite_name"
        ),
    )

    benchmark_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    case_revision_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    suite: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)


class Candidate(Base):
    __tablename__ = "candidate"
    __table_args__ = (
        Index("ix_candidate_status", "tenant_id", "project_id", "benchmark_id", "status"),
        CheckConstraint(
            "status IN ('proposed','validated','approved','rejected','published',"
            "'needs_review','invalid')",
            name="candidate_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    benchmark_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    case_revision_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="proposed")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    observed_outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    expected_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    evidence_refs: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    review_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    split: Mapped[str | None] = mapped_column(String(32), nullable=True)  # developer | holdout
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class Experiment(Base):
    __tablename__ = "experiment"
    __table_args__ = (
        Index("ix_experiment_project", "tenant_id", "project_id", "status"),
        CheckConstraint(
            "status IN ('queued','running','completed','partial_failed','canceled',"
            "'budget_exhausted')",
            name="experiment_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    benchmark_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    baseline_release_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    candidate_release_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="queued")
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    suites: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    evaluators: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    gate_decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # pass | fail | inconclusive
    report: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class Execution(Base):
    __tablename__ = "execution"
    __table_args__ = (
        UniqueConstraint(
            "experiment_id",
            "case_revision_id",
            "variant",
            "replicate",
            name="uq_execution_key",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    experiment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    case_revision_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    variant: Mapped[str] = mapped_column(String(32), nullable=False)  # baseline | candidate
    replicate: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="queued")
    failure_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # agent | infrastructure | transport
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    wall_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fixture_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    submission_key: Mapped[str | None] = mapped_column(String(256), nullable=True)


class Score(Base):
    __tablename__ = "score"
    __table_args__ = (
        UniqueConstraint(
            "experiment_id",
            "case_revision_id",
            "variant",
            "replicate",
            "evaluator",
            name="uq_score_key",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    experiment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    execution_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    case_revision_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    variant: Mapped[str] = mapped_column(String(32), nullable=False)
    replicate: Mapped[int] = mapped_column(Integer, default=0)
    evaluator: Mapped[str] = mapped_column(String(128), nullable=False)
    evaluator_version: Mapped[str] = mapped_column(String(32), default="1.0")
    value: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    missing: Mapped[bool] = mapped_column(Boolean, default=False)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_ids: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, server_default=text("'{}'::jsonb")
    )


class Report(Base):
    __tablename__ = "report"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    experiment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, unique=True)
    structured: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    narrative: Mapped[str | None] = mapped_column(Text, nullable=True)
    gate_decision: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


class Job(Base):
    __tablename__ = "job"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "project_id", "logical_key", name="uq_job_logical"
        ),
        Index("ix_job_claim", "status", "due_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    logical_key: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="queued")
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fencing_token: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=8)
    outcome: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    checkpoint: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    initiating_principal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    policy_revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Checkpoint(Base):
    __tablename__ = "checkpoint"
    __table_args__ = (UniqueConstraint("job_id", "fencing_token", name="uq_checkpoint_fence"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    fencing_token: Mapped[int] = mapped_column(Integer, nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditEvent(Base):
    __tablename__ = "audit_event"
    __table_args__ = (
        Index("ix_audit_scope", "tenant_id", "project_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(128), nullable=False)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DailyMetric(Base):
    __tablename__ = "daily_metric"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "agent_id",
            "metric",
            "local_date",
            "dimension_key",
            name="uq_daily_metric",
        ),
        Index("ix_daily_metric_query", "tenant_id", "project_id", "metric", "local_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    local_date: Mapped[date] = mapped_column(Date, nullable=False)
    dimension_key: Mapped[str] = mapped_column(String(256), default="")
    dimensions: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    numerator: Mapped[Decimal | None] = mapped_column(Numeric(24, 8), nullable=True)
    denominator: Mapped[Decimal | None] = mapped_column(Numeric(24, 8), nullable=True)
    value: Mapped[Decimal | None] = mapped_column(Numeric(24, 8), nullable=True)
    known_count: Mapped[int] = mapped_column(Integer, default=0)
    unknown_count: Mapped[int] = mapped_column(Integer, default=0)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    source_completeness: Mapped[Decimal | None] = mapped_column(Numeric(8, 6), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_record"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "project_id", "operation", "idempotency_key", name="uq_idempotency"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    operation: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RefreshWatermark(Base):
    __tablename__ = "refresh_watermark"
    __table_args__ = (
        UniqueConstraint("tenant_id", "project_id", "benchmark_id", name="uq_refresh_wm"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    benchmark_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    watermark: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PolicyCacheVersion(Base):
    """Monotonic policy revision per project for cache invalidation."""

    __tablename__ = "policy_cache_version"
    __table_args__ = (
        UniqueConstraint("tenant_id", "project_id", name="uq_policy_cache"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
