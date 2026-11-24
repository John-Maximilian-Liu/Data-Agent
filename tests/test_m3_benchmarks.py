"""Milestone 3 — QA import v1, refresh candidates, reproducible manifests."""

from __future__ import annotations

from pathlib import Path

from dataagent.benchmarks.service import (
    get_manifest,
    import_qa,
    publish_version_from_candidates,
    refresh_benchmark,
    review_candidate,
)
from dataagent.ingestion.pipeline import SourceAuth, ingest_batch
from dataagent.storage import models as m
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]


def test_seed_import_creates_v1(session, world):
    bench = m.Benchmark(
        tenant_id=world.tenant_a.id,
        project_id=world.project_a1.id,
        slug="core",
        name="Core",
        ownership="agent_scoped",
        owner_agent_id=world.agents[0].id,
    )
    session.add(bench)
    session.flush()
    content = (ROOT / "fixtures/qa/seed.jsonl").read_text()
    result = import_qa(
        session,
        tenant_id=world.tenant_a.id,
        project_id=world.project_a1.id,
        benchmark_id=bench.id,
        content=content,
        fmt="jsonl",
        actor_id=world.admin.id,
    )
    assert result.valid and result.version == 1
    manifest = get_manifest(session, result.version_id)
    assert len(manifest["cases"]) == 3
    assert manifest["manifest_hash"]
    # cases without component annotations still valid


def _seed_benchmark(session, world):
    bench = m.Benchmark(
        tenant_id=world.tenant_a.id,
        project_id=world.project_a1.id,
        slug="core-refresh",
        name="Core",
        ownership="agent_scoped",
        owner_agent_id=world.agents[0].id,
    )
    session.add(bench)
    session.flush()
    content = (ROOT / "fixtures/qa/seed.jsonl").read_text()
    result = import_qa(
        session,
        tenant_id=world.tenant_a.id,
        project_id=world.project_a1.id,
        benchmark_id=bench.id,
        content=content,
        fmt="jsonl",
        actor_id=world.admin.id,
    )
    assert result.valid and result.version == 1
    return bench, result


def test_refresh_and_v2_preserves_v1(session, world):
    bench, v1 = _seed_benchmark(session, world)
    # ingest traffic with Q&A metadata
    auth = SourceAuth(
        tenant_id=world.tenant_a.id,
        project_id=world.project_a1.id,
        source_id=world.source.id,
        allowed_agent_ids={world.agents[0].id},
        timezone="UTC",
    )
    events = [
        {
            "event_id": "rf-1",
            "event_type": "session.start",
            "schema_version": "1.0",
            "occurred_at": "2024-07-01T10:00:00Z",
            "agent_id": str(world.agents[0].id),
            "payload": {"session_id": "rf-s1", "user_id": "u9"},
        },
        {
            "event_id": "rf-2",
            "event_type": "session.end",
            "schema_version": "1.0",
            "occurred_at": "2024-07-01T10:05:00Z",
            "agent_id": str(world.agents[0].id),
            "payload": {
                "session_id": "rf-s1",
                "state": "completed",
                "business_success": True,
                "technical_completed": True,
            },
        },
        {
            "event_id": "rf-3",
            "event_type": "feedback.revision",
            "schema_version": "1.0",
            "occurred_at": "2024-07-01T10:06:00Z",
            "agent_id": str(world.agents[0].id),
            "payload": {
                "subject_type": "session",
                "subject_id": "rf-s1",
                "rater_id": "u9",
                "scale": "stars",
                "score": 1,
            },
        },
    ]
    ingest_batch(session, auth, events)
    sess = session.execute(
        select(m.Session).where(m.Session.external_session_id == "rf-s1")
    ).scalar_one()
    sess.metadata_ = {"question": "New prod Q?", "answer": "maybe"}

    out = refresh_benchmark(
        session,
        tenant_id=world.tenant_a.id,
        project_id=world.project_a1.id,
        benchmark_id=bench.id,
        actor_id=world.admin.id,
    )
    assert out["candidate_count"] >= 1
    cands = list(
        session.execute(select(m.Candidate).where(m.Candidate.benchmark_id == bench.id)).scalars()
    )
    # low rating => needs_review, no expected answer from production
    low = [c for c in cands if c.review_reason == "low_rating"]
    assert low
    assert all(c.expected_answer is None for c in low)

    review_candidate(
        session,
        tenant_id=world.tenant_a.id,
        project_id=world.project_a1.id,
        candidate_id=cands[0].id,
        decision="approved",
        expected_revision=cands[0].revision,
        actor_id=world.admin.id,
        expected_answer="approved answer",
    )
    v2 = publish_version_from_candidates(
        session,
        tenant_id=world.tenant_a.id,
        project_id=world.project_a1.id,
        benchmark_id=bench.id,
        actor_id=world.admin.id,
    )
    assert v2.version == 2
    m1 = get_manifest(session, v1.version_id)
    m2 = get_manifest(session, v2.id)
    assert m1["manifest_hash"] != m2["manifest_hash"] or len(m2["cases"]) >= len(m1["cases"])
    # v1 still runnable
    assert len(m1["cases"]) == 3


def test_validate_only_preview(session, world):
    bench = m.Benchmark(
        tenant_id=world.tenant_a.id,
        project_id=world.project_a1.id,
        slug="preview",
        name="Preview",
    )
    session.add(bench)
    session.flush()
    bad = '{"question":"","expected_answer":"x"}\n'
    result = import_qa(
        session,
        tenant_id=world.tenant_a.id,
        project_id=world.project_a1.id,
        benchmark_id=bench.id,
        content=bad,
        fmt="jsonl",
        actor_id=world.admin.id,
        validate_only=True,
    )
    assert not result.valid
    assert result.issues
