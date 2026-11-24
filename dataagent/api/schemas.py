"""Pydantic request/response schemas."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field


class ErrorBody(BaseModel):
    code: str
    message: str | None = None
    request_id: str | None = None


class AgentCreate(BaseModel):
    slug: str = Field(min_length=1, max_length=128)
    name: str
    owner_team: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentUpdate(BaseModel):
    name: str | None = None
    owner_team: str | None = None
    status: str | None = None
    metadata: dict[str, Any] | None = None


class AgentOut(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    status: str
    owner_team: str | None = None


class AgentGroupCreate(BaseModel):
    slug: str
    name: str


class MemberAdd(BaseModel):
    agent_id: uuid.UUID


class RoleBindingCreate(BaseModel):
    principal_id: uuid.UUID
    role_name: str
    agent_id: uuid.UUID | None = None
    agent_group_id: uuid.UUID | None = None
    whole_project: bool = False
    reason: str | None = None
    expires_at: datetime | None = None


class SourceCreate(BaseModel):
    slug: str
    name: str
    kind: str = "native"
    allowed_agent_ids: list[uuid.UUID] = Field(default_factory=list)
    single_agent_id: uuid.UUID | None = None
    capabilities: dict[str, Any] = Field(default_factory=dict)


class MappingRevisionCreate(BaseModel):
    mapping: dict[str, Any]
    publish: bool = False
    dry_run_samples: list[dict[str, Any]] | None = None


class EventBatch(BaseModel):
    events: list[dict[str, Any]] = Field(max_length=500)


class FeedbackIn(BaseModel):
    event_id: str
    occurred_at: datetime
    agent_id: uuid.UUID | None = None
    payload: dict[str, Any]


class MetricsQuery(BaseModel):
    metric: str
    start: date
    end: date
    agent_ids: list[uuid.UUID] | None = None
    top_n: int | None = 10


class BenchmarkCreate(BaseModel):
    slug: str
    name: str
    ownership: str = "agent_scoped"
    owner_agent_id: uuid.UUID | None = None


class QAImportBody(BaseModel):
    content: str
    format: str = "jsonl"
    validate_only: bool = False
    suite: str = "fixed_core"


class RefreshBody(BaseModel):
    dry_run: bool = False
    agent_ids: list[uuid.UUID] | None = None


class CandidateReview(BaseModel):
    decision: str
    expected_revision: int
    expected_answer: str | None = None
    reason: str | None = None


class ExperimentCreate(BaseModel):
    agent_id: uuid.UUID
    benchmark_version_id: uuid.UUID
    baseline_release_id: uuid.UUID
    candidate_release_id: uuid.UUID
    suites: list[str] | None = None
    evaluators: list[str] | None = None
    config: dict[str, Any] | None = None


class ReleaseCreate(BaseModel):
    agent_id: uuid.UUID
    version: str
    endpoint_url: str | None = None
    ci_target: str | None = None
    artifact_versions: dict[str, Any] = Field(default_factory=dict)
    is_production: bool = False


class SettingsUpdate(BaseModel):
    modules: dict[str, bool] | None = None
    settings: dict[str, Any] | None = None


class CredentialCreate(BaseModel):
    name: str
    kind: str
    scopes: list[str] = Field(default_factory=list)
    agent_id: uuid.UUID | None = None
    allowed_agent_ids: list[uuid.UUID] = Field(default_factory=list)


class MembershipCreate(BaseModel):
    principal_id: uuid.UUID
    project_id: uuid.UUID | None = None


class CISubmission(BaseModel):
    case_revision_id: uuid.UUID
    variant: str
    replicate: int = 0
    submission_key: str
    output: dict[str, Any]


class Page(BaseModel):
    items: list[Any]
    next_cursor: str | None = None
    total: int | None = None
