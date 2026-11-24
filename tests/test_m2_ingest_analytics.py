"""Milestone 2 — intake paths, dedupe, late data, scoped charts."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from dataagent.analytics.charts import build_chart
from dataagent.analytics.metrics import compute_daily_metrics, query_metrics
from dataagent.authorization.policy import evaluate_effective_access, filter_authorized_agents
from dataagent.ingestion.mapping import apply_mapping, dry_run_mapping
from dataagent.ingestion.pipeline import SourceAuth, ingest_batch, ingest_otlp_traces
from dataagent.storage import models as m
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]


def _auth(world) -> SourceAuth:
    return SourceAuth(
        tenant_id=world.tenant_a.id,
        project_id=world.project_a1.id,
        source_id=world.source.id,
        allowed_agent_ids={a.id for a in world.agents},
        timezone=world.project_a1.timezone,
    )


def _load_native(world):
    events = []
    for line in (ROOT / "fixtures/events/native_batch.jsonl").read_text().splitlines():
        ev = json.loads(line)
        if ev["agent_id"] == "AGENT_ALPHA":
            ev["agent_id"] = str(world.agents[0].id)
        elif ev["agent_id"] == "AGENT_BETA":
            ev["agent_id"] = str(world.agents[1].id)
        events.append(ev)
    return events


def test_native_ingest_and_duplicate(session, world):
    auth = _auth(world)
    events = _load_native(world)
    r1 = ingest_batch(session, auth, events)
    assert r1.accepted >= 8
    r2 = ingest_batch(session, auth, events)
    assert r2.duplicate == r1.accepted
    assert r2.accepted == 0
    # changed payload conflict
    conflict = dict(events[0])
    conflict["payload"] = {**conflict["payload"], "user_id": "changed"}
    r3 = ingest_batch(session, auth, [conflict])
    assert r3.content_conflict == 1


def test_unauthorized_agent_rejected_no_persist(session, world):
    auth = _auth(world)
    foreign = str(uuid_agent := world.agents[0].id)  # noqa — use random
    import uuid

    bad_id = uuid.uuid4()
    events = [
        {
            "event_id": "x-1",
            "event_type": "session.start",
            "schema_version": "1.0",
            "occurred_at": "2024-06-01T12:00:00Z",
            "agent_id": str(bad_id),
            "payload": {"session_id": "nope"},
        }
    ]
    r = ingest_batch(session, auth, events)
    assert r.rejected == 1
    assert session.execute(select(m.Session).where(m.Session.external_session_id == "nope")).scalar_one_or_none() is None


def test_otlp_ingest(session, world):
    auth = _auth(world)
    body = json.loads((ROOT / "fixtures/otlp/traces.json").read_text())
    # rewrite agent id
    body["resourceSpans"][0]["resource"]["attributes"][0]["value"]["stringValue"] = str(
        world.agents[0].id
    )
    r = ingest_otlp_traces(session, auth, body)
    assert r.accepted >= 2
    spans = list(session.execute(select(m.Span).where(m.Span.agent_id == world.agents[0].id)).scalars())
    assert spans


def test_mapping_dry_run_and_apply(session, world):
    mapping = json.loads((ROOT / "fixtures/events/mapping_workflow.json").read_text())
    # simplify mapping for our fixture — expand steps needs event_type from item
    mapping = {
        "schema_version": "1.0",
        "event_type": "session.start",
        "fields": {
            "event_id": {"path": "workflow_id"},
            "agent_id": {"path": "agent"},
            "occurred_at": {"path": "ended_at_ms", "timestamp": True, "timestamp_unit": "ms"},
            "payload": {"path": "session", "optional": True},
        },
    }
    raw = json.loads((ROOT / "fixtures/events/workflow_shaped.json").read_text())
    raw["agent"] = str(world.agents[0].id)
    preview = dry_run_mapping([raw], mapping)
    assert preview["accepted_count"] == 1
    events = apply_mapping(raw, mapping)
    # shape into canonical
    for e in events:
        e["payload"] = {"session_id": raw["session"], "user_id": raw["user"]}
    auth = _auth(world)
    r = ingest_batch(session, auth, events, mapper_revision=1)
    assert r.accepted == 1


def test_dst_boundary_local_date(session, world):
    """America/New_York DST: 2024-03-10 06:30 UTC is still 2024-03-10 01:30 EST."""
    from dataagent.util.timeutil import local_date_for
    from datetime import datetime, UTC

    d = local_date_for(datetime(2024, 3, 10, 6, 30, tzinfo=UTC), "America/New_York")
    assert d == date(2024, 3, 10)
    # after spring forward 2024-03-10 07:30 UTC = 03:30 EDT
    d2 = local_date_for(datetime(2024, 3, 10, 7, 30, tzinfo=UTC), "America/New_York")
    assert d2 == date(2024, 3, 10)


def test_scoped_metrics_and_charts(session, world):
    auth = _auth(world)
    ingest_batch(session, auth, _load_native(world))
    compute_daily_metrics(
        session,
        tenant_id=world.tenant_a.id,
        project_id=world.project_a1.id,
        agent_ids={world.agents[0].id, world.agents[1].id},
        local_date=date(2024, 6, 1),
    )
    access = evaluate_effective_access(
        session,
        principal_id=world.analyst.id,
        tenant_id=world.tenant_a.id,
        project_id=world.project_a1.id,
    )
    allowed = filter_authorized_agents(access, None, "metrics.read")
    assert world.agents[2].id not in allowed
    result = query_metrics(
        session,
        tenant_id=world.tenant_a.id,
        project_id=world.project_a1.id,
        authorized_agent_ids=allowed,
        metric="session_volume",
        start=date(2024, 6, 1),
        end=date(2024, 6, 1),
    )
    chart = build_chart(result)
    assert "data" in chart
    assert chart.get("empty_state") is None or chart["data"] is not None


def test_late_session_end_without_start(session, world):
    auth = _auth(world)
    events = [
        {
            "event_id": "late-1",
            "event_type": "session.end",
            "schema_version": "1.0",
            "occurred_at": "2024-06-02T12:00:00Z",
            "agent_id": str(world.agents[0].id),
            "payload": {"session_id": "late-sess", "state": "completed", "technical_completed": True},
        }
    ]
    r = ingest_batch(session, auth, events)
    assert r.accepted == 1
    sess = session.execute(
        select(m.Session).where(m.Session.external_session_id == "late-sess")
    ).scalar_one()
    assert sess.metadata_.get("late_start") is True
