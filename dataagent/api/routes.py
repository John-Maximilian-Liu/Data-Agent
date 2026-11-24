"""All /v1/projects/{project_id}/... route families."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query
from sqlalchemy import func, select

from dataagent.analytics.charts import build_chart
from dataagent.analytics.metrics import query_metrics
from dataagent.api.deps import Ctx, DbSession, check_idempotency, store_idempotency
from dataagent.api import schemas as s
from dataagent.authorization.policy import (
    AuthorizationError,
    authorize,
    bump_policy_revision,
    filter_authorized_agents,
)
from dataagent.benchmarks.service import (
    ConflictError,
    get_manifest,
    import_qa,
    publish_version_from_candidates,
    refresh_benchmark,
    review_candidate,
)
from dataagent.experiments.runner import (
    cancel_experiment,
    start_experiment,
    submit_ci_result,
)
from dataagent.ingestion.mapping import dry_run_mapping
from dataagent.ingestion.pipeline import SourceAuth, ingest_batch, ingest_otlp_traces
from dataagent.jobs.queue import JobQueue
from dataagent.storage import models as m
from dataagent.storage.models import DEFAULT_MODULES
from dataagent.util.hashing import new_credential_token, secret_hash

router = APIRouter(prefix="/v1/projects/{project_id}")


def _forbid(e: AuthorizationError) -> None:
    raise HTTPException(status_code=403, detail={"code": e.code, "message": e.message})


def _audit(session, ctx: Ctx, action: str, resource_type: str, resource_id: str, **meta: Any) -> None:
    session.add(
        m.AuditEvent(
            tenant_id=ctx.auth.tenant_id,
            project_id=ctx.project.id,
            actor_id=ctx.auth.principal_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            metadata_=meta,
        )
    )


# ----- Agents -----


@router.post("/agents", status_code=201)
def create_agent(body: s.AgentCreate, ctx: Ctx, session: DbSession):
    try:
        authorize(ctx.access, "agent.register")
    except AuthorizationError as e:
        _forbid(e)
    existing = session.execute(
        select(m.Agent).where(
            m.Agent.tenant_id == ctx.auth.tenant_id,
            m.Agent.project_id == ctx.project.id,
            m.Agent.slug == body.slug,
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail={"code": "slug_exists"})
    agent = m.Agent(
        tenant_id=ctx.auth.tenant_id,
        project_id=ctx.project.id,
        slug=body.slug,
        name=body.name,
        owner_team=body.owner_team,
        metadata_=body.metadata,
    )
    session.add(agent)
    session.flush()
    _audit(session, ctx, "agent.create", "agent", str(agent.id))
    return {"id": agent.id, "slug": agent.slug, "name": agent.name, "status": agent.status}


@router.get("/agents")
def list_agents(
    ctx: Ctx,
    session: DbSession,
    status: str | None = None,
    q: str | None = None,
    cursor: int = 0,
    limit: int = Query(50, le=200),
):
    try:
        authorize(ctx.access, "metrics.read")
    except AuthorizationError:
        # still allow listing agents the caller can see via bindings
        pass
    allowed = ctx.access.authorized_agent_ids() | (
        set(ctx.access.by_agent.keys()) if ctx.access.project_actions else set()
    )
    if "agent.register" in ctx.access.project_actions or "project.admin" in ctx.access.project_actions:
        allowed = None  # see all
    query = select(m.Agent).where(
        m.Agent.tenant_id == ctx.auth.tenant_id,
        m.Agent.project_id == ctx.project.id,
    )
    if status:
        query = query.where(m.Agent.status == status)
    if q:
        query = query.where(m.Agent.name.ilike(f"%{q}%") | m.Agent.slug.ilike(f"%{q}%"))
    if allowed is not None:
        if not allowed and "project.admin" not in ctx.access.tenant_actions:
            # owners with no bindings yet — empty
            query = query.where(m.Agent.id.in_([]))
        else:
            query = query.where(m.Agent.id.in_(list(allowed)))
    query = query.order_by(m.Agent.slug).offset(cursor).limit(limit)
    items = [
        {"id": a.id, "slug": a.slug, "name": a.name, "status": a.status, "owner_team": a.owner_team}
        for a in session.execute(query).scalars()
    ]
    return {"items": items, "next_cursor": str(cursor + limit) if len(items) == limit else None}


@router.get("/agents/{agent_id}")
def get_agent(agent_id: uuid.UUID, ctx: Ctx, session: DbSession):
    agent = session.get(m.Agent, agent_id)
    if (
        agent is None
        or agent.tenant_id != ctx.auth.tenant_id
        or agent.project_id != ctx.project.id
    ):
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    if not ctx.access.can("metrics.read", agent_id) and not ctx.access.can(
        "agent.update", agent_id
    ):
        raise HTTPException(status_code=403, detail={"code": "forbidden"})
    return {
        "id": agent.id,
        "slug": agent.slug,
        "name": agent.name,
        "status": agent.status,
        "owner_team": agent.owner_team,
        "metadata": agent.metadata_,
    }


@router.patch("/agents/{agent_id}")
def update_agent(agent_id: uuid.UUID, body: s.AgentUpdate, ctx: Ctx, session: DbSession):
    try:
        authorize(ctx.access, "agent.update", agent_id=agent_id)
    except AuthorizationError as e:
        _forbid(e)
    agent = session.get(m.Agent, agent_id)
    if agent is None or agent.project_id != ctx.project.id:
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    if body.name is not None:
        agent.name = body.name
    if body.owner_team is not None:
        agent.owner_team = body.owner_team
    if body.status is not None:
        if body.status not in ("active", "paused", "archived"):
            raise HTTPException(status_code=400, detail={"code": "bad_status"})
        if body.status == "paused":
            authorize(ctx.access, "agent.pause", agent_id=agent_id)
        if body.status == "archived":
            authorize(ctx.access, "agent.archive", agent_id=agent_id)
        agent.status = body.status
    if body.metadata is not None:
        agent.metadata_ = body.metadata
    _audit(session, ctx, "agent.update", "agent", str(agent.id), status=agent.status)
    return {"id": agent.id, "status": agent.status}


# ----- Agent groups -----


@router.post("/agent-groups", status_code=201)
def create_group(body: s.AgentGroupCreate, ctx: Ctx, session: DbSession):
    try:
        authorize(ctx.access, "project.admin")
    except AuthorizationError as e:
        _forbid(e)
    g = m.AgentGroup(
        tenant_id=ctx.auth.tenant_id,
        project_id=ctx.project.id,
        slug=body.slug,
        name=body.name,
    )
    session.add(g)
    session.flush()
    return {"id": g.id, "slug": g.slug, "name": g.name}


@router.post("/agent-groups/{group_id}/members", status_code=201)
def add_group_member(group_id: uuid.UUID, body: s.MemberAdd, ctx: Ctx, session: DbSession):
    try:
        authorize(ctx.access, "project.admin")
    except AuthorizationError as e:
        _forbid(e)
    g = session.get(m.AgentGroup, group_id)
    agent = session.get(m.Agent, body.agent_id)
    if not g or not agent or g.project_id != ctx.project.id or agent.project_id != ctx.project.id:
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    session.add(
        m.AgentGroupMembership(
            tenant_id=ctx.auth.tenant_id,
            project_id=ctx.project.id,
            group_id=group_id,
            agent_id=body.agent_id,
        )
    )
    rev = bump_policy_revision(session, ctx.auth.tenant_id, ctx.project.id)
    _audit(session, ctx, "group.member.add", "agent_group", str(group_id), policy_revision=rev)
    return {"ok": True, "policy_revision": rev}


@router.delete("/agent-groups/{group_id}/members/{agent_id}")
def remove_group_member(
    group_id: uuid.UUID, agent_id: uuid.UUID, ctx: Ctx, session: DbSession
):
    try:
        authorize(ctx.access, "project.admin")
    except AuthorizationError as e:
        _forbid(e)
    row = session.execute(
        select(m.AgentGroupMembership).where(
            m.AgentGroupMembership.group_id == group_id,
            m.AgentGroupMembership.agent_id == agent_id,
            m.AgentGroupMembership.project_id == ctx.project.id,
        )
    ).scalar_one_or_none()
    if row:
        session.delete(row)
        bump_policy_revision(session, ctx.auth.tenant_id, ctx.project.id)
    return {"ok": True}


# ----- Role bindings / access -----


@router.post("/role-bindings", status_code=201)
def create_binding(body: s.RoleBindingCreate, ctx: Ctx, session: DbSession):
    try:
        authorize(ctx.access, "role.bind")
    except AuthorizationError as e:
        _forbid(e)
    role = session.execute(
        select(m.Role).where(m.Role.name == body.role_name)
    ).scalars().first()
    if role is None:
        raise HTTPException(status_code=400, detail={"code": "unknown_role"})
    selectors = sum(
        [
            body.agent_id is not None,
            body.agent_group_id is not None,
            body.whole_project,
        ]
    )
    if selectors != 1:
        raise HTTPException(status_code=400, detail={"code": "selector_required"})
    if body.whole_project and "project.admin" not in ctx.access.project_actions:
        # cannot expand to whole-project unless caller can delegate that scope
        if "project.admin" not in ctx.access.tenant_actions:
            raise HTTPException(status_code=403, detail={"code": "delegation_ceiling"})
    binding = m.ScopedRoleBinding(
        tenant_id=ctx.auth.tenant_id,
        project_id=ctx.project.id,
        principal_id=body.principal_id,
        role_id=role.id,
        scope_level="project",
        agent_id=body.agent_id,
        agent_group_id=body.agent_group_id,
        whole_project=body.whole_project,
        grantor_id=ctx.auth.principal_id,
        reason=body.reason,
        expires_at=body.expires_at,
    )
    session.add(binding)
    session.flush()
    rev = bump_policy_revision(session, ctx.auth.tenant_id, ctx.project.id)
    _audit(session, ctx, "role.bind", "scoped_role_binding", str(binding.id), policy_revision=rev)
    return {"id": binding.id, "policy_revision": rev}


@router.delete("/role-bindings/{binding_id}")
def revoke_binding(binding_id: uuid.UUID, ctx: Ctx, session: DbSession):
    try:
        authorize(ctx.access, "role.revoke")
    except AuthorizationError as e:
        _forbid(e)
    binding = session.get(m.ScopedRoleBinding, binding_id)
    if not binding or binding.project_id != ctx.project.id:
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    binding.status = "revoked"
    from datetime import UTC, datetime

    binding.revoked_at = datetime.now(UTC)
    rev = bump_policy_revision(session, ctx.auth.tenant_id, ctx.project.id)
    # cancel pending jobs for affected agent if agent-scoped
    if binding.agent_id:
        JobQueue(session, "api").cancel_pending_for_principal_scope(
            tenant_id=ctx.auth.tenant_id,
            project_id=ctx.project.id,
            agent_ids_to_cancel={binding.agent_id},
        )
    return {"ok": True, "policy_revision": rev}


@router.get("/access/effective")
def effective_access(ctx: Ctx, principal_id: uuid.UUID | None = None):
    target = principal_id or ctx.auth.principal_id
    if target != ctx.auth.principal_id:
        try:
            authorize(ctx.access, "project.admin")
        except AuthorizationError as e:
            _forbid(e)
    # return caller's access when self
    access = ctx.access
    return {
        "principal_id": str(access.principal_id),
        "policy_revision": access.policy_revision,
        "modules": access.modules,
        "project_actions": sorted(access.project_actions),
        "project_fields": sorted(access.project_fields),
        "agents": {
            str(aid): {
                "actions": sorted(ent.actions),
                "fields": sorted(ent.fields),
            }
            for aid, ent in access.by_agent.items()
        },
    }


@router.get("/roles")
def list_roles(ctx: Ctx, session: DbSession):
    rows = list(session.execute(select(m.Role)).scalars())
    return {
        "items": [
            {"id": r.id, "name": r.name, "actions": r.actions, "fields": r.fields}
            for r in rows
        ]
    }


@router.post("/memberships", status_code=201)
def add_membership(body: s.MembershipCreate, ctx: Ctx, session: DbSession):
    try:
        authorize(ctx.access, "membership.manage")
    except AuthorizationError as e:
        _forbid(e)
    row = m.Membership(
        tenant_id=ctx.auth.tenant_id,
        project_id=body.project_id or ctx.project.id,
        principal_id=body.principal_id,
        status="active",
    )
    session.add(row)
    session.flush()
    return {"id": row.id}


@router.get("/memberships")
def list_memberships(ctx: Ctx, session: DbSession):
    try:
        authorize(ctx.access, "membership.manage")
    except AuthorizationError as e:
        _forbid(e)
    rows = session.execute(
        select(m.Membership).where(m.Membership.tenant_id == ctx.auth.tenant_id)
    ).scalars()
    return {
        "items": [
            {
                "id": r.id,
                "principal_id": r.principal_id,
                "project_id": r.project_id,
                "status": r.status,
            }
            for r in rows
        ]
    }


# ----- Settings -----


@router.get("/settings")
def get_settings_route(ctx: Ctx):
    return {"modules": ctx.project.modules, "settings": ctx.project.settings, "timezone": ctx.project.timezone}


@router.patch("/settings")
def patch_settings(body: s.SettingsUpdate, ctx: Ctx, session: DbSession):
    try:
        authorize(ctx.access, "settings.update")
    except AuthorizationError as e:
        _forbid(e)
    if body.modules is not None:
        modules = dict(ctx.project.modules or DEFAULT_MODULES)
        modules.update(body.modules)
        ctx.project.modules = modules
    if body.settings is not None:
        settings = dict(ctx.project.settings or {})
        settings.update(body.settings)
        ctx.project.settings = settings
    return {"modules": ctx.project.modules, "settings": ctx.project.settings}


# ----- Sources / mapping / credentials -----


@router.post("/sources", status_code=201)
def create_source(body: s.SourceCreate, ctx: Ctx, session: DbSession):
    try:
        authorize(ctx.access, "source.register")
    except AuthorizationError as e:
        _forbid(e)
    src = m.Source(
        tenant_id=ctx.auth.tenant_id,
        project_id=ctx.project.id,
        slug=body.slug,
        name=body.name,
        kind=body.kind,
        allowed_agent_ids=[str(a) for a in body.allowed_agent_ids],
        single_agent_id=body.single_agent_id,
        capabilities=body.capabilities,
    )
    session.add(src)
    session.flush()
    return {"id": src.id, "slug": src.slug}


@router.get("/sources")
def list_sources(ctx: Ctx, session: DbSession):
    rows = session.execute(
        select(m.Source).where(
            m.Source.tenant_id == ctx.auth.tenant_id,
            m.Source.project_id == ctx.project.id,
        )
    ).scalars()
    return {"items": [{"id": r.id, "slug": r.slug, "kind": r.kind, "status": r.status} for r in rows]}


@router.post("/sources/{source_id}/mapping-revisions", status_code=201)
def create_mapping(
    source_id: uuid.UUID, body: s.MappingRevisionCreate, ctx: Ctx, session: DbSession
):
    try:
        authorize(ctx.access, "mapping.publish")
    except AuthorizationError as e:
        _forbid(e)
    src = session.get(m.Source, source_id)
    if not src or src.project_id != ctx.project.id:
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    if body.dry_run_samples is not None:
        preview = dry_run_mapping(body.dry_run_samples, body.mapping)
        if not body.publish:
            return {"dry_run": preview}
    prev = session.execute(
        select(func.max(m.MappingRevision.revision)).where(m.MappingRevision.source_id == source_id)
    ).scalar() or 0
    rev = m.MappingRevision(
        tenant_id=ctx.auth.tenant_id,
        project_id=ctx.project.id,
        source_id=source_id,
        revision=prev + 1,
        status="published" if body.publish else "draft",
        mapping=body.mapping,
    )
    session.add(rev)
    session.flush()
    return {"id": rev.id, "revision": rev.revision, "status": rev.status}


@router.post("/sources/{source_id}/credentials", status_code=201)
def create_credential(
    source_id: uuid.UUID, body: s.CredentialCreate, ctx: Ctx, session: DbSession
):
    try:
        authorize(ctx.access, "credential.manage")
    except AuthorizationError as e:
        _forbid(e)
    token = new_credential_token()
    row = m.ServiceCredential(
        tenant_id=ctx.auth.tenant_id,
        project_id=ctx.project.id,
        name=body.name,
        kind=body.kind,
        secret_hash=secret_hash(token),
        agent_id=body.agent_id,
        source_id=source_id,
        allowed_agent_ids=[str(a) for a in body.allowed_agent_ids],
        scopes=body.scopes,
    )
    session.add(row)
    session.flush()
    # raw token returned once only
    return {"id": row.id, "token": token, "kind": row.kind}


# ----- Ingestion -----


def _source_auth(session, ctx: Ctx, source_id: uuid.UUID) -> SourceAuth:
    src = session.get(m.Source, source_id)
    if not src or src.project_id != ctx.project.id or src.tenant_id != ctx.auth.tenant_id:
        raise HTTPException(status_code=404, detail={"code": "source_not_found"})
    allowed = {uuid.UUID(x) for x in (src.allowed_agent_ids or [])}
    if src.single_agent_id:
        allowed.add(src.single_agent_id)
    return SourceAuth(
        tenant_id=ctx.auth.tenant_id,
        project_id=ctx.project.id,
        source_id=src.id,
        allowed_agent_ids=allowed,
        single_agent_id=src.single_agent_id,
        timezone=ctx.project.timezone or "UTC",
    )


@router.post("/events:batch")
def events_batch(
    body: s.EventBatch,
    ctx: Ctx,
    session: DbSession,
    source_id: uuid.UUID = Query(...),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    try:
        authorize(ctx.access, "source.write")
    except AuthorizationError as e:
        # service credential path may only have source.write on agents
        if "source.write" not in ctx.auth.scopes and ctx.auth.kind != "service":
            _forbid(e)
    cached = check_idempotency(
        session,
        tenant_id=ctx.auth.tenant_id,
        project_id=ctx.project.id,
        operation="events:batch",
        key=idempotency_key,
        body=body.model_dump(mode="json"),
    )
    if cached:
        return cached["body"]
    auth = _source_auth(session, ctx, source_id)
    result = ingest_batch(session, auth, body.events)
    resp = {
        "accepted": result.accepted,
        "duplicate": result.duplicate,
        "rejected": result.rejected,
        "content_conflict": result.content_conflict,
        "items": [item.__dict__ for item in result.items],
    }
    store_idempotency(
        session,
        tenant_id=ctx.auth.tenant_id,
        project_id=ctx.project.id,
        operation="events:batch",
        key=idempotency_key,
        body=body.model_dump(mode="json"),
        status=200,
        response=resp,
    )
    return resp


@router.post("/feedback")
def post_feedback(body: s.FeedbackIn, ctx: Ctx, session: DbSession, source_id: uuid.UUID = Query(...)):
    event = {
        "event_id": body.event_id,
        "event_type": "feedback.revision",
        "schema_version": "1.0",
        "occurred_at": body.occurred_at.isoformat(),
        "agent_id": str(body.agent_id) if body.agent_id else None,
        "payload": body.payload,
    }
    auth = _source_auth(session, ctx, source_id)
    result = ingest_batch(session, auth, [event])
    return {"items": [i.__dict__ for i in result.items]}


@router.post("/otlp/v1/traces")
def otlp_traces(request_body: dict[str, Any], ctx: Ctx, session: DbSession, source_id: uuid.UUID = Query(...)):
    auth = _source_auth(session, ctx, source_id)
    result = ingest_otlp_traces(session, auth, request_body)
    return {
        "accepted": result.accepted,
        "duplicate": result.duplicate,
        "rejected": result.rejected,
        "content_conflict": result.content_conflict,
    }


@router.post("/sources/{source_id}/webhook")
def source_webhook(
    source_id: uuid.UUID, payload: dict[str, Any], ctx: Ctx, session: DbSession
):
    auth = _source_auth(session, ctx, source_id)
    rev = session.execute(
        select(m.MappingRevision)
        .where(
            m.MappingRevision.source_id == source_id,
            m.MappingRevision.status == "published",
        )
        .order_by(m.MappingRevision.revision.desc())
    ).scalars().first()
    if rev is None:
        raise HTTPException(status_code=400, detail={"code": "no_published_mapping"})
    from dataagent.ingestion.mapping import apply_mapping, MappingError

    events = []
    raw_items = payload if isinstance(payload, list) else [payload]
    for raw in raw_items:
        try:
            events.extend(apply_mapping(raw, rev.mapping))
        except MappingError:
            continue
    result = ingest_batch(session, auth, events, mapper_revision=rev.revision)
    return {"accepted": result.accepted, "rejected": result.rejected, "duplicate": result.duplicate}


# ----- Analytics -----


@router.get("/analytics/metrics")
def analytics_metrics(
    ctx: Ctx,
    session: DbSession,
    metric: str,
    start: date,
    end: date,
    agent_ids: list[uuid.UUID] | None = Query(None),
    top_n: int | None = 10,
):
    try:
        authorize(ctx.access, "metrics.read")
    except AuthorizationError as e:
        _forbid(e)
    from dataagent.analytics.metrics import METRIC_REGISTRY

    defn = METRIC_REGISTRY.get(metric)
    if defn and defn.requires_field == "cost":
        requested = set(agent_ids) if agent_ids else ctx.access.authorized_agent_ids("metrics.read")
        allowed = {
            aid
            for aid in requested
            if ctx.access.can_action_and_field("metrics.read", "cost", aid)
        }
    else:
        allowed = filter_authorized_agents(
            ctx.access, set(agent_ids) if agent_ids else None, "metrics.read"
        )
    return query_metrics(
        session,
        tenant_id=ctx.auth.tenant_id,
        project_id=ctx.project.id,
        authorized_agent_ids=allowed,
        metric=metric,
        start=start,
        end=end,
        top_n=top_n,
    )


@router.get("/analytics/charts/{metric}")
def analytics_chart(
    metric: str,
    ctx: Ctx,
    session: DbSession,
    start: date,
    end: date,
    agent_ids: list[uuid.UUID] | None = Query(None),
):
    data = analytics_metrics(ctx, session, metric, start, end, agent_ids)
    return build_chart(data)


# ----- Releases -----


@router.post("/releases", status_code=201)
def create_release(body: s.ReleaseCreate, ctx: Ctx, session: DbSession):
    try:
        authorize(ctx.access, "agent.update", agent_id=body.agent_id)
    except AuthorizationError as e:
        _forbid(e)
    rel = m.Release(
        tenant_id=ctx.auth.tenant_id,
        project_id=ctx.project.id,
        agent_id=body.agent_id,
        version=body.version,
        endpoint_url=body.endpoint_url,
        ci_target=body.ci_target,
        artifact_versions=body.artifact_versions,
        is_production=body.is_production,
    )
    session.add(rel)
    session.flush()
    return {"id": rel.id, "version": rel.version}


# ----- Benchmarks -----


@router.post("/benchmarks", status_code=201)
def create_benchmark(body: s.BenchmarkCreate, ctx: Ctx, session: DbSession):
    try:
        authorize(ctx.access, "benchmark.create")
    except AuthorizationError as e:
        _forbid(e)
    b = m.Benchmark(
        tenant_id=ctx.auth.tenant_id,
        project_id=ctx.project.id,
        slug=body.slug,
        name=body.name,
        ownership=body.ownership,
        owner_agent_id=body.owner_agent_id,
    )
    session.add(b)
    session.flush()
    return {"id": b.id, "slug": b.slug}


@router.post("/benchmarks/{benchmark_id}/qa-imports")
def qa_import(benchmark_id: uuid.UUID, body: s.QAImportBody, ctx: Ctx, session: DbSession):
    try:
        authorize(ctx.access, "benchmark.import")
    except AuthorizationError as e:
        _forbid(e)
    result = import_qa(
        session,
        tenant_id=ctx.auth.tenant_id,
        project_id=ctx.project.id,
        benchmark_id=benchmark_id,
        content=body.content,
        fmt=body.format,
        actor_id=ctx.auth.principal_id,
        validate_only=body.validate_only,
        suite=body.suite,
    )
    if not result.valid:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "validation_failed",
                "issues": [i.__dict__ for i in result.issues],
            },
        )
    return {
        "valid": True,
        "case_count": result.case_count,
        "version_id": result.version_id,
        "version": result.version,
        "artifact_hash": result.artifact_hash,
    }


@router.post("/benchmarks/{benchmark_id}/refreshes", status_code=202)
def trigger_refresh(
    benchmark_id: uuid.UUID, body: s.RefreshBody, ctx: Ctx, session: DbSession
):
    try:
        authorize(ctx.access, "benchmark.refresh")
    except AuthorizationError as e:
        _forbid(e)
    if body.dry_run:
        return refresh_benchmark(
            session,
            tenant_id=ctx.auth.tenant_id,
            project_id=ctx.project.id,
            benchmark_id=benchmark_id,
            actor_id=ctx.auth.principal_id,
            dry_run=True,
            participating_agent_ids=set(body.agent_ids) if body.agent_ids else None,
        )
    queue = JobQueue(session, "api")
    job = queue.enqueue(
        tenant_id=ctx.auth.tenant_id,
        project_id=ctx.project.id,
        kind="benchmark.refresh",
        logical_key=f"refresh:{benchmark_id}:{uuid.uuid4()}",
        payload={
            "benchmark_id": str(benchmark_id),
            "agent_ids": [str(a) for a in body.agent_ids] if body.agent_ids else None,
        },
        initiating_principal_id=ctx.auth.principal_id,
        policy_revision=ctx.access.policy_revision,
    )
    return {
        "job_id": job.id,
        "status": job.status,
        "status_url": f"/v1/projects/{ctx.project.id}/jobs/{job.id}",
    }


@router.get("/benchmarks/{benchmark_id}/candidates")
def list_candidates(benchmark_id: uuid.UUID, ctx: Ctx, session: DbSession):
    try:
        authorize(ctx.access, "benchmark.review")
    except AuthorizationError as e:
        _forbid(e)
    rows = session.execute(
        select(m.Candidate).where(
            m.Candidate.benchmark_id == benchmark_id,
            m.Candidate.project_id == ctx.project.id,
        )
    ).scalars()
    return {
        "items": [
            {
                "id": c.id,
                "status": c.status,
                "revision": c.revision,
                "question": c.question[:200],
                "split": c.split,
                "review_reason": c.review_reason,
                # holdout answers not exposed here
                "has_expected": c.expected_answer is not None,
            }
            for c in rows
        ]
    }


@router.post("/benchmarks/{benchmark_id}/candidates/{candidate_id}/review")
def review(
    benchmark_id: uuid.UUID,
    candidate_id: uuid.UUID,
    body: s.CandidateReview,
    ctx: Ctx,
    session: DbSession,
):
    try:
        authorize(ctx.access, "benchmark.review")
    except AuthorizationError as e:
        _forbid(e)
    try:
        cand = review_candidate(
            session,
            tenant_id=ctx.auth.tenant_id,
            project_id=ctx.project.id,
            candidate_id=candidate_id,
            decision=body.decision,
            expected_revision=body.expected_revision,
            actor_id=ctx.auth.principal_id,
            expected_answer=body.expected_answer,
            reason=body.reason,
        )
    except ConflictError:
        raise HTTPException(status_code=409, detail={"code": "optimistic_concurrency"})
    except LookupError:
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    return {"id": cand.id, "status": cand.status, "revision": cand.revision}


@router.post("/benchmarks/{benchmark_id}/versions", status_code=201)
def publish_version(benchmark_id: uuid.UUID, ctx: Ctx, session: DbSession):
    try:
        authorize(ctx.access, "benchmark.publish")
    except AuthorizationError as e:
        _forbid(e)
    version = publish_version_from_candidates(
        session,
        tenant_id=ctx.auth.tenant_id,
        project_id=ctx.project.id,
        benchmark_id=benchmark_id,
        actor_id=ctx.auth.principal_id,
    )
    return {"id": version.id, "version": version.version, "manifest_hash": version.manifest_hash}


@router.get("/benchmarks/{benchmark_id}/versions/{version_id}")
def get_version(
    benchmark_id: uuid.UUID, version_id: uuid.UUID, ctx: Ctx, session: DbSession
):
    try:
        authorize(ctx.access, "metrics.read")
    except AuthorizationError as e:
        _forbid(e)
    manifest = get_manifest(session, version_id)
    # strip holdout question content — only hashes
    for c in manifest["cases"]:
        if c.get("holdout") and not ctx.access.can_field("holdout", ctx.access.by_agent and next(iter(ctx.access.by_agent), uuid.UUID(int=0))):
            c.pop("question", None)
    return manifest


# ----- Experiments -----


@router.post("/experiments", status_code=202)
def create_experiment(body: s.ExperimentCreate, ctx: Ctx, session: DbSession):
    try:
        authorize(ctx.access, "experiment.create", agent_id=body.agent_id)
    except AuthorizationError as e:
        _forbid(e)
    exp = start_experiment(
        session,
        tenant_id=ctx.auth.tenant_id,
        project_id=ctx.project.id,
        agent_id=body.agent_id,
        benchmark_version_id=body.benchmark_version_id,
        baseline_release_id=body.baseline_release_id,
        candidate_release_id=body.candidate_release_id,
        created_by=ctx.auth.principal_id,
        suites=body.suites,
        evaluators=body.evaluators,
        config=body.config,
    )
    queue = JobQueue(session, "api")
    job = queue.enqueue(
        tenant_id=ctx.auth.tenant_id,
        project_id=ctx.project.id,
        agent_id=body.agent_id,
        kind="experiment.run",
        logical_key=f"experiment:{exp.id}",
        payload={"experiment_id": str(exp.id)},
        initiating_principal_id=ctx.auth.principal_id,
        policy_revision=ctx.access.policy_revision,
    )
    return {
        "id": exp.id,
        "job_id": job.id,
        "status": exp.status,
        "status_url": f"/v1/projects/{ctx.project.id}/jobs/{job.id}",
    }


@router.get("/experiments/{experiment_id}")
def get_experiment(experiment_id: uuid.UUID, ctx: Ctx, session: DbSession):
    exp = session.get(m.Experiment, experiment_id)
    if not exp or exp.project_id != ctx.project.id:
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    try:
        authorize(ctx.access, "experiment.read", agent_id=exp.agent_id)
    except AuthorizationError as e:
        _forbid(e)
    return {
        "id": exp.id,
        "status": exp.status,
        "gate_decision": exp.gate_decision,
        "suites": exp.suites,
        "evaluators": exp.evaluators,
    }


@router.get("/experiments/{experiment_id}/results")
def experiment_results(experiment_id: uuid.UUID, ctx: Ctx, session: DbSession):
    exp = session.get(m.Experiment, experiment_id)
    if not exp or exp.project_id != ctx.project.id:
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    try:
        authorize(ctx.access, "experiment.read", agent_id=exp.agent_id)
    except AuthorizationError as e:
        _forbid(e)
    scores = session.execute(
        select(m.Score).where(m.Score.experiment_id == experiment_id)
    ).scalars()
    items = []
    for sc in scores:
        item = {
            "case_revision_id": sc.case_revision_id,
            "variant": sc.variant,
            "evaluator": sc.evaluator,
            "value": float(sc.value) if sc.value is not None else None,
            "missing": sc.missing,
        }
        if ctx.access.can_field("judge_detail", exp.agent_id):
            item["explanation"] = sc.explanation
        items.append(item)
    return {"items": items}


@router.get("/experiments/{experiment_id}/report")
def experiment_report(experiment_id: uuid.UUID, ctx: Ctx, session: DbSession):
    exp = session.get(m.Experiment, experiment_id)
    if not exp or exp.project_id != ctx.project.id:
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    try:
        authorize(ctx.access, "report.read", agent_id=exp.agent_id)
    except AuthorizationError as e:
        _forbid(e)
    report = session.execute(
        select(m.Report).where(m.Report.experiment_id == experiment_id)
    ).scalar_one_or_none()
    structured = (report.structured if report else exp.report) or {}
    # redact holdout / evidence without field grant
    if not ctx.access.can_field("evidence", exp.agent_id):
        structured = {**structured, "critical_regressions": [
            {k: v for k, v in r.items() if k != "evidence"}
            for r in structured.get("critical_regressions") or []
        ]}
    return {
        "gate_decision": (report.gate_decision if report else exp.gate_decision),
        "structured": structured,
        "narrative": report.narrative if report and ctx.access.can_field("raw_text", exp.agent_id) else None,
    }


@router.post("/experiments/{experiment_id}/cancel")
def experiment_cancel(experiment_id: uuid.UUID, ctx: Ctx, session: DbSession):
    exp = session.get(m.Experiment, experiment_id)
    if not exp or exp.project_id != ctx.project.id:
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    try:
        authorize(ctx.access, "experiment.cancel", agent_id=exp.agent_id)
    except AuthorizationError as e:
        _forbid(e)
    cancel_experiment(session, experiment_id)
    return {"id": experiment_id, "status": "canceled"}


@router.post("/experiments/{experiment_id}/ci-results", status_code=201)
def ci_results(experiment_id: uuid.UUID, body: s.CISubmission, ctx: Ctx, session: DbSession):
    if "ci.submit" not in ctx.auth.scopes and not ctx.access.can("ci.submit"):
        raise HTTPException(status_code=403, detail={"code": "forbidden"})
    exp = session.get(m.Experiment, experiment_id)
    if not exp or exp.project_id != ctx.project.id:
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    row = submit_ci_result(
        session,
        tenant_id=ctx.auth.tenant_id,
        project_id=ctx.project.id,
        experiment_id=experiment_id,
        case_revision_id=body.case_revision_id,
        variant=body.variant,
        replicate=body.replicate,
        submission_key=body.submission_key,
        output=body.output,
    )
    return {"id": row.id, "status": row.status}


# ----- Jobs -----


@router.get("/jobs/{job_id}")
def get_job(job_id: uuid.UUID, ctx: Ctx, session: DbSession):
    job = session.get(m.Job, job_id)
    if not job or job.project_id != ctx.project.id or job.tenant_id != ctx.auth.tenant_id:
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    try:
        authorize(ctx.access, "job.read", agent_id=job.agent_id)
    except AuthorizationError as e:
        _forbid(e)
    return {
        "id": job.id,
        "kind": job.kind,
        "status": job.status,
        "attempts": job.attempts,
        "outcome": job.outcome,
        "checkpoint": job.checkpoint,
        "error": job.error,
    }
