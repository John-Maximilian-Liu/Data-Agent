"""Canonical event ingestion with receipt dedupe and scoped agent validation."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from dataagent.config import get_settings
from dataagent.storage import models as m
from dataagent.util.hashing import content_hash, hmac_pseudonym
from dataagent.util.timeutil import local_date_for, parse_utc

CANONICAL_TYPES = {
    "session.start",
    "session.end",
    "run.completed",
    "feedback.revision",
    "model.call",
    "retrieval",
    "tool.call",
    "skill.execution",
    "span",
}


@dataclass
class ItemResult:
    index: int
    status: str  # accepted | duplicate | rejected | content_conflict
    event_id: str | None = None
    diagnostic_code: str | None = None
    resource_id: str | None = None


@dataclass
class IngestResult:
    accepted: int = 0
    duplicate: int = 0
    rejected: int = 0
    content_conflict: int = 0
    items: list[ItemResult] = field(default_factory=list)


@dataclass
class SourceAuth:
    tenant_id: uuid.UUID
    project_id: uuid.UUID
    source_id: uuid.UUID
    allowed_agent_ids: set[uuid.UUID]
    single_agent_id: uuid.UUID | None = None
    timezone: str = "UTC"


def _stable_payload_hash(event: dict[str, Any]) -> str:
    return content_hash(json.dumps(event, sort_keys=True, default=str))


def _resolve_agent_id(event: dict[str, Any], auth: SourceAuth) -> uuid.UUID | None:
    if auth.single_agent_id is not None:
        return auth.single_agent_id
    raw = event.get("agent_id") or (event.get("payload") or {}).get("agent_id")
    if raw is None:
        return None
    try:
        aid = uuid.UUID(str(raw))
    except ValueError:
        return None
    if aid not in auth.allowed_agent_ids:
        return None
    return aid


def ingest_batch(
    session: Session,
    auth: SourceAuth,
    events: list[dict[str, Any]],
    *,
    mapper_revision: int | None = None,
) -> IngestResult:
    """Process mixed batches item by item; rejected rows are not written."""
    result = IngestResult()
    settings = get_settings()
    pepper = settings.hmac_pepper.get_secret_value()

    # Load paused/archived agents to block intake
    agent_status = {
        row.id: row.status
        for row in session.execute(
            select(m.Agent).where(
                m.Agent.tenant_id == auth.tenant_id,
                m.Agent.project_id == auth.project_id,
            )
        ).scalars()
    }

    for idx, event in enumerate(events):
        item = _ingest_one(
            session,
            auth,
            event,
            index=idx,
            pepper=pepper,
            agent_status=agent_status,
            mapper_revision=mapper_revision,
        )
        result.items.append(item)
        setattr(result, item.status, getattr(result, item.status) + 1)

    session.flush()
    return result


def _ingest_one(
    session: Session,
    auth: SourceAuth,
    event: dict[str, Any],
    *,
    index: int,
    pepper: str,
    agent_status: dict[uuid.UUID, str],
    mapper_revision: int | None,
) -> ItemResult:
    event_id = str(event.get("event_id") or "")
    if not event_id:
        return ItemResult(index=index, status="rejected", diagnostic_code="missing_event_id")

    event_type = event.get("event_type")
    if event_type not in CANONICAL_TYPES:
        return ItemResult(
            index=index, status="rejected", event_id=event_id, diagnostic_code="unknown_type"
        )

    occurred_at = event.get("occurred_at")
    if not occurred_at:
        return ItemResult(
            index=index, status="rejected", event_id=event_id, diagnostic_code="missing_occurred_at"
        )

    agent_id = _resolve_agent_id(event, auth)
    if agent_id is None:
        # Do not leak whether agent exists outside grant
        return ItemResult(
            index=index, status="rejected", event_id=event_id, diagnostic_code="agent_not_permitted"
        )

    status = agent_status.get(agent_id)
    if status is None or status == "paused" or status == "archived":
        return ItemResult(
            index=index, status="rejected", event_id=event_id, diagnostic_code="agent_not_accepting"
        )

    payload_hash = _stable_payload_hash(event)
    existing = session.execute(
        select(m.Receipt).where(
            m.Receipt.tenant_id == auth.tenant_id,
            m.Receipt.project_id == auth.project_id,
            m.Receipt.source_id == auth.source_id,
            m.Receipt.external_event_id == event_id,
        )
    ).scalar_one_or_none()

    if existing is not None:
        if existing.content_hash == payload_hash:
            return ItemResult(
                index=index, status="duplicate", event_id=event_id, diagnostic_code="duplicate"
            )
        return ItemResult(
            index=index,
            status="content_conflict",
            event_id=event_id,
            diagnostic_code="content_conflict",
        )

    try:
        resource_id = _project_event(
            session,
            auth,
            event,
            agent_id=agent_id,
            pepper=pepper,
            mapper_revision=mapper_revision,
        )
    except Exception:
        return ItemResult(
            index=index, status="rejected", event_id=event_id, diagnostic_code="projection_error"
        )

    session.add(
        m.Receipt(
            tenant_id=auth.tenant_id,
            project_id=auth.project_id,
            source_id=auth.source_id,
            external_event_id=event_id,
            content_hash=payload_hash,
            status="accepted",
            received_at=datetime.now(UTC),
        )
    )
    return ItemResult(
        index=index, status="accepted", event_id=event_id, resource_id=resource_id
    )


def _project_event(
    session: Session,
    auth: SourceAuth,
    event: dict[str, Any],
    *,
    agent_id: uuid.UUID,
    pepper: str,
    mapper_revision: int | None,
) -> str:
    et = event["event_type"]
    payload = event.get("payload") or {}
    occurred = parse_utc(event["occurred_at"])
    local_d = local_date_for(occurred, auth.timezone)
    traffic = payload.get("traffic_tag") or event.get("traffic_tag") or "production"

    if et == "session.start":
        ext = str(payload.get("session_id") or event.get("session_id"))
        user_raw = payload.get("user_id")
        pseudo = (
            hmac_pseudonym(str(user_raw), pepper, str(auth.project_id)) if user_raw else None
        )
        existing = session.execute(
            select(m.Session).where(
                m.Session.tenant_id == auth.tenant_id,
                m.Session.project_id == auth.project_id,
                m.Session.agent_id == agent_id,
                m.Session.external_session_id == ext,
            )
        ).scalar_one_or_none()
        if existing:
            return str(existing.id)
        row = m.Session(
            tenant_id=auth.tenant_id,
            project_id=auth.project_id,
            agent_id=agent_id,
            external_session_id=ext,
            user_pseudonym=pseudo,
            state="started",
            traffic_tag=traffic,
            started_at=occurred,
            local_date=local_d,
            metadata_=payload.get("metadata") or {},
        )
        session.add(row)
        session.flush()
        return str(row.id)

    if et == "session.end":
        ext = str(payload.get("session_id") or event.get("session_id"))
        row = session.execute(
            select(m.Session).where(
                m.Session.tenant_id == auth.tenant_id,
                m.Session.project_id == auth.project_id,
                m.Session.agent_id == agent_id,
                m.Session.external_session_id == ext,
            )
        ).scalar_one_or_none()
        state = payload.get("state") or "completed"
        if row is None:
            row = m.Session(
                tenant_id=auth.tenant_id,
                project_id=auth.project_id,
                agent_id=agent_id,
                external_session_id=ext,
                state=state,
                traffic_tag=traffic,
                started_at=occurred,
                ended_at=occurred,
                local_date=local_d,
                technical_completed=payload.get("technical_completed"),
                business_success=payload.get("business_success"),
                metadata_={"late_start": True},
            )
            session.add(row)
        else:
            row.state = state
            row.ended_at = occurred
            row.technical_completed = payload.get("technical_completed")
            row.business_success = payload.get("business_success")
        session.flush()
        return str(row.id)

    if et == "run.completed":
        ext = str(payload.get("run_id") or event.get("run_id"))
        session_ext = payload.get("session_id")
        session_id = None
        unresolved = False
        if session_ext:
            srow = session.execute(
                select(m.Session).where(
                    m.Session.tenant_id == auth.tenant_id,
                    m.Session.project_id == auth.project_id,
                    m.Session.agent_id == agent_id,
                    m.Session.external_session_id == str(session_ext),
                )
            ).scalar_one_or_none()
            if srow:
                session_id = srow.id
            else:
                unresolved = True
        wall = payload.get("wall_ms")
        row = m.Run(
            tenant_id=auth.tenant_id,
            project_id=auth.project_id,
            agent_id=agent_id,
            session_id=session_id,
            external_run_id=ext,
            trace_id=payload.get("trace_id"),
            status=payload.get("status") or "completed",
            started_at=parse_utc(payload["started_at"]) if payload.get("started_at") else occurred,
            ended_at=occurred,
            wall_ms=wall,
            error_code=payload.get("error_code"),
            release_version=payload.get("release_version"),
            unresolved_parent=unresolved,
            metadata_=payload.get("metadata") or {},
        )
        session.add(row)
        session.flush()
        return str(row.id)

    if et == "feedback.revision":
        subject_type = payload.get("subject_type") or "session"
        subject_id = str(payload.get("subject_id"))
        rater = payload.get("rater_id") or payload.get("user_id") or "anonymous"
        pseudo = hmac_pseudonym(str(rater), pepper, str(auth.project_id))
        scale = str(payload.get("scale") or "thumbs")
        # latest effective wins — store new revision
        prev = session.execute(
            select(m.FeedbackRevision)
            .where(
                m.FeedbackRevision.tenant_id == auth.tenant_id,
                m.FeedbackRevision.project_id == auth.project_id,
                m.FeedbackRevision.agent_id == agent_id,
                m.FeedbackRevision.subject_type == subject_type,
                m.FeedbackRevision.subject_id == subject_id,
                m.FeedbackRevision.rater_pseudonym == pseudo,
                m.FeedbackRevision.scale == scale,
            )
            .order_by(m.FeedbackRevision.revision.desc())
        ).scalars().first()
        rev = (prev.revision + 1) if prev else 1
        score = payload.get("score")
        row = m.FeedbackRevision(
            tenant_id=auth.tenant_id,
            project_id=auth.project_id,
            agent_id=agent_id,
            subject_type=subject_type,
            subject_id=subject_id,
            rater_pseudonym=pseudo,
            scale=scale,
            score=Decimal(str(score)) if score is not None else None,
            retracted=bool(payload.get("retracted")),
            revision=rev,
            effective_at=occurred,
            comment_redacted=payload.get("comment_redacted"),
        )
        session.add(row)
        session.flush()
        return str(row.id)

    if et == "model.call":
        call_id = payload.get("provider_call_id")
        if call_id:
            existing = session.execute(
                select(m.ModelUsage).where(
                    m.ModelUsage.tenant_id == auth.tenant_id,
                    m.ModelUsage.project_id == auth.project_id,
                    m.ModelUsage.provider_call_id == str(call_id),
                )
            ).scalar_one_or_none()
            if existing:
                return str(existing.id)
        row = m.ModelUsage(
            tenant_id=auth.tenant_id,
            project_id=auth.project_id,
            agent_id=agent_id,
            provider_call_id=str(call_id) if call_id else None,
            model=str(payload.get("model") or "unknown"),
            deployment=payload.get("deployment"),
            occurred_at=occurred,
            local_date=local_d,
            input_tokens=int(payload.get("input_tokens") or 0),
            output_tokens=int(payload.get("output_tokens") or 0),
            cache_tokens=int(payload.get("cache_tokens") or 0),
            usage_class=payload.get("usage_class") or "production",
            cost_usd=(
                Decimal(str(payload["cost_usd"])) if payload.get("cost_usd") is not None else None
            ),
            cost_estimated=bool(payload.get("cost_estimated")),
            rate_unknown=bool(payload.get("rate_unknown")),
        )
        # apply price schedule if no cost
        if row.cost_usd is None:
            price = _lookup_price(session, row.model, row.deployment, local_d)
            if price:
                row.cost_usd = (
                    Decimal(row.input_tokens) / 1000 * price.input_per_1k
                    + Decimal(row.output_tokens) / 1000 * price.output_per_1k
                    + Decimal(row.cache_tokens) / 1000 * price.cache_per_1k
                )
                row.cost_estimated = True
            else:
                row.rate_unknown = True
        session.add(row)
        session.flush()
        return str(row.id)

    if et in ("retrieval", "tool.call", "skill.execution"):
        kind = {"retrieval": "kb", "tool.call": "tool", "skill.execution": "skill"}[et]
        component_id = str(payload.get("component_id") or payload.get("name") or "unknown")
        row = m.Activity(
            tenant_id=auth.tenant_id,
            project_id=auth.project_id,
            agent_id=agent_id,
            kind=kind,
            component_id=component_id,
            version=payload.get("version"),
            occurred_at=occurred,
            local_date=local_d,
            success=payload.get("success"),
            is_citation=bool(payload.get("is_citation")),
            attributes=payload.get("attributes") or {},
        )
        session.add(row)
        session.flush()
        return str(row.id)

    if et == "span":
        trace_id = str(payload.get("trace_id") or event.get("trace_id"))
        span_id = str(payload.get("span_id") or event.get("span_id"))
        parent = payload.get("parent_span_id")
        unresolved = False
        if parent:
            parent_row = session.execute(
                select(m.Span).where(
                    m.Span.tenant_id == auth.tenant_id,
                    m.Span.project_id == auth.project_id,
                    m.Span.trace_id == trace_id,
                    m.Span.span_id == str(parent),
                )
            ).scalar_one_or_none()
            unresolved = parent_row is None
        row = m.Span(
            tenant_id=auth.tenant_id,
            project_id=auth.project_id,
            agent_id=agent_id,
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=str(parent) if parent else None,
            name=str(payload.get("name") or "span"),
            kind=payload.get("kind") or "internal",
            started_at=parse_utc(payload["started_at"]) if payload.get("started_at") else occurred,
            ended_at=occurred,
            status=payload.get("status") or "ok",
            component_id=payload.get("component_id"),
            unresolved_parent=unresolved,
            attributes=payload.get("attributes") or {},
            mapper_revision=mapper_revision,
        )
        session.add(row)
        # resolve children waiting on this span
        children = session.execute(
            select(m.Span).where(
                m.Span.tenant_id == auth.tenant_id,
                m.Span.project_id == auth.project_id,
                m.Span.trace_id == trace_id,
                m.Span.parent_span_id == span_id,
                m.Span.unresolved_parent.is_(True),
            )
        ).scalars()
        for child in children:
            child.unresolved_parent = False
        session.flush()
        return str(row.id)

    raise ValueError(f"unhandled type {et}")


def _lookup_price(
    session: Session, model: str, deployment: str | None, on_date
) -> m.PriceSchedule | None:
    q = (
        select(m.PriceSchedule)
        .where(
            m.PriceSchedule.model == model,
            m.PriceSchedule.effective_from <= on_date,
        )
        .order_by(m.PriceSchedule.effective_from.desc())
    )
    rows = list(session.execute(q).scalars())
    if deployment:
        for r in rows:
            if r.deployment == deployment:
                return r
    return rows[0] if rows else None


def ingest_otlp_traces(
    session: Session, auth: SourceAuth, otlp_body: dict[str, Any]
) -> IngestResult:
    """Map OTLP/HTTP JSON traces into canonical span/model events."""
    events: list[dict[str, Any]] = []
    for rs in otlp_body.get("resourceSpans") or []:
        resource_attrs = {
            a["key"]: list(a.get("value", {}).values())[0]
            for a in (rs.get("resource") or {}).get("attributes") or []
            if "value" in a
        }
        agent_id = resource_attrs.get("dataagent.agent_id") or resource_attrs.get("agent.id")
        for ss in rs.get("scopeSpans") or []:
            for span in ss.get("spans") or []:
                start_ns = int(span.get("startTimeUnixNano") or 0)
                end_ns = int(span.get("endTimeUnixNano") or start_ns)
                occurred = datetime.fromtimestamp(end_ns / 1e9, tz=UTC).isoformat()
                attrs = {
                    a["key"]: list(a.get("value", {}).values())[0]
                    for a in span.get("attributes") or []
                    if "value" in a
                }
                events.append(
                    {
                        "event_id": f"otlp:{span.get('traceId')}:{span.get('spanId')}",
                        "event_type": "span",
                        "schema_version": "1.0",
                        "occurred_at": occurred,
                        "agent_id": agent_id,
                        "payload": {
                            "trace_id": span.get("traceId"),
                            "span_id": span.get("spanId"),
                            "parent_span_id": span.get("parentSpanId") or None,
                            "name": span.get("name"),
                            "kind": attrs.get("dataagent.span.kind") or "internal",
                            "started_at": datetime.fromtimestamp(start_ns / 1e9, tz=UTC).isoformat(),
                            "status": (span.get("status") or {}).get("code", "ok"),
                            "component_id": attrs.get("dataagent.component_id"),
                            "attributes": attrs,
                        },
                    }
                )
                if attrs.get("gen_ai.usage.input_tokens") or attrs.get("llm.token_count.prompt"):
                    events.append(
                        {
                            "event_id": f"otlp-usage:{span.get('traceId')}:{span.get('spanId')}",
                            "event_type": "model.call",
                            "schema_version": "1.0",
                            "occurred_at": occurred,
                            "agent_id": agent_id,
                            "payload": {
                                "provider_call_id": attrs.get("gen_ai.response.id")
                                or f"{span.get('traceId')}:{span.get('spanId')}",
                                "model": attrs.get("gen_ai.request.model")
                                or attrs.get("llm.request.model")
                                or "unknown",
                                "input_tokens": int(
                                    attrs.get("gen_ai.usage.input_tokens")
                                    or attrs.get("llm.token_count.prompt")
                                    or 0
                                ),
                                "output_tokens": int(
                                    attrs.get("gen_ai.usage.output_tokens")
                                    or attrs.get("llm.token_count.completion")
                                    or 0
                                ),
                            },
                        }
                    )
    return ingest_batch(session, auth, events)
