"""Synthetic offline demo seed and pipeline walkthrough."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from dataagent.analytics.metrics import compute_daily_metrics
from dataagent.benchmarks.service import import_qa
from dataagent.experiments.runner import run_experiment, start_experiment
from dataagent.ingestion.pipeline import SourceAuth, ingest_batch
from dataagent.storage.database import init_engine, session_scope
from dataagent.storage import models as m
from dataagent.storage.models import DEFAULT_MODULES
from dataagent.api.app import seed_system_roles


def run_demo() -> dict:
    init_engine()
    with session_scope() as session:
        seed_system_roles(session)

        t1 = m.Tenant(slug="acme", name="Acme")
        t2 = m.Tenant(slug="globex", name="Globex")
        session.add_all([t1, t2])
        session.flush()

        p1 = m.Project(tenant_id=t1.id, slug="support", name="Support", timezone="America/New_York", modules=dict(DEFAULT_MODULES))
        p2 = m.Project(tenant_id=t1.id, slug="sales", name="Sales", timezone="UTC", modules=dict(DEFAULT_MODULES))
        p3 = m.Project(tenant_id=t2.id, slug="ops", name="Ops", timezone="UTC", modules=dict(DEFAULT_MODULES))
        session.add_all([p1, p2, p3])
        session.flush()

        admin = m.Principal(issuer="offline", subject="demo-admin", display_name="Demo Admin")
        analyst = m.Principal(issuer="offline", subject="demo-analyst", display_name="Demo Analyst")
        session.add_all([admin, analyst])
        session.flush()
        session.add_all(
            [
                m.Membership(tenant_id=t1.id, project_id=p1.id, principal_id=admin.id),
                m.Membership(tenant_id=t1.id, project_id=p1.id, principal_id=analyst.id),
                m.Membership(tenant_id=t1.id, project_id=p2.id, principal_id=admin.id),
                m.Membership(tenant_id=t2.id, project_id=p3.id, principal_id=admin.id),
            ]
        )

        roles = {r.name: r for r in session.execute(select(m.Role)).scalars()}
        agents = []
        for i, slug in enumerate(["alpha", "beta", "gamma", "delta"]):
            a = m.Agent(
                tenant_id=t1.id,
                project_id=p1.id,
                slug=slug,
                name=slug.title(),
                owner_team="team-a" if i < 2 else "team-b",
            )
            session.add(a)
            agents.append(a)
        session.flush()

        # admin whole-project; analyst only on alpha with metrics, beta with traces (no cost mashup)
        session.add(
            m.ScopedRoleBinding(
                tenant_id=t1.id,
                project_id=p1.id,
                principal_id=admin.id,
                role_id=roles["project_admin"].id,
                scope_level="project",
                whole_project=True,
            )
        )
        session.add(
            m.ScopedRoleBinding(
                tenant_id=t1.id,
                project_id=p1.id,
                principal_id=analyst.id,
                role_id=roles["analyst"].id,
                scope_level="project",
                agent_id=agents[0].id,
            )
        )
        # give analyst cost on alpha only via custom binding using project_admin fields? use agent_developer without cost
        session.add(
            m.PolicyCacheVersion(tenant_id=t1.id, project_id=p1.id, revision=1)
        )

        src = m.Source(
            tenant_id=t1.id,
            project_id=p1.id,
            slug="collector",
            name="Collector",
            kind="native",
            allowed_agent_ids=[str(a.id) for a in agents],
        )
        session.add(src)
        session.flush()

        # ingest synthetic events
        fixtures = Path(__file__).resolve().parents[1] / "fixtures" / "events" / "native_batch.jsonl"
        events = []
        now = datetime.now(UTC)
        if fixtures.exists():
            for line in fixtures.read_text().splitlines():
                if line.strip():
                    events.append(json.loads(line))
        else:
            for a in agents[:2]:
                sid = f"s-{a.slug}-1"
                events.append(
                    {
                        "event_id": f"e-{a.slug}-start",
                        "event_type": "session.start",
                        "schema_version": "1.0",
                        "occurred_at": (now - timedelta(hours=2)).isoformat(),
                        "agent_id": str(a.id),
                        "payload": {"session_id": sid, "user_id": f"user-{a.slug}"},
                    }
                )
                events.append(
                    {
                        "event_id": f"e-{a.slug}-end",
                        "event_type": "session.end",
                        "schema_version": "1.0",
                        "occurred_at": (now - timedelta(hours=1)).isoformat(),
                        "agent_id": str(a.id),
                        "payload": {
                            "session_id": sid,
                            "state": "completed",
                            "technical_completed": True,
                            "business_success": True,
                            "question": f"What is {a.slug}?",
                            "answer": f"{a.slug} agent",
                        },
                    }
                )

        # rewrite agent ids in fixture events if placeholders
        for ev in events:
            if ev.get("agent_id") in (None, "AGENT_ALPHA"):
                ev["agent_id"] = str(agents[0].id)
            elif ev.get("agent_id") == "AGENT_BETA":
                ev["agent_id"] = str(agents[1].id)

        auth = SourceAuth(
            tenant_id=t1.id,
            project_id=p1.id,
            source_id=src.id,
            allowed_agent_ids={a.id for a in agents},
            timezone=p1.timezone,
        )
        # attach metadata question/answer onto sessions via payload in session.end — pipeline stores metadata_ only on start
        # patch: put question into session.start metadata for refresh
        for ev in events:
            if ev["event_type"] == "session.end":
                # also update after ingest
                pass
        result = ingest_batch(session, auth, events)
        # enrich sessions with Q&A metadata for refresh
        for sess in session.execute(select(m.Session).where(m.Session.project_id == p1.id)).scalars():
            meta = dict(sess.metadata_ or {})
            meta.setdefault("question", f"Q for {sess.external_session_id}")
            meta.setdefault("answer", "A1")
            sess.metadata_ = meta

        # recompute using stored session local_dates
        dates = {s.local_date for s in session.execute(select(m.Session)).scalars()}
        for d in dates:
            compute_daily_metrics(
                session,
                tenant_id=t1.id,
                project_id=p1.id,
                agent_ids={a.id for a in agents},
                local_date=d,
            )

        bench = m.Benchmark(
            tenant_id=t1.id, project_id=p1.id, slug="core", name="Core", ownership="agent_scoped",
            owner_agent_id=agents[0].id,
        )
        session.add(bench)
        session.flush()
        qa = (
            '{"external_case_id":"c1","question":"What is 2+2?","expected_answer":"4"}\n'
            '{"external_case_id":"c2","question":"Capital of France?","expected_answer":"Paris",'
            '"permitted_tools":["search"],"document_labels":["doc1"]}\n'
        )
        imp = import_qa(
            session,
            tenant_id=t1.id,
            project_id=p1.id,
            benchmark_id=bench.id,
            content=qa,
            fmt="jsonl",
            actor_id=admin.id,
        )

        baseline = m.Release(
            tenant_id=t1.id, project_id=p1.id, agent_id=agents[0].id, version="1.0.0", is_production=True
        )
        candidate = m.Release(
            tenant_id=t1.id, project_id=p1.id, agent_id=agents[0].id, version="1.1.0-regress"
        )
        session.add_all([baseline, candidate])
        session.flush()

        exp = start_experiment(
            session,
            tenant_id=t1.id,
            project_id=p1.id,
            agent_id=agents[0].id,
            benchmark_version_id=imp.version_id,
            baseline_release_id=baseline.id,
            candidate_release_id=candidate.id,
            created_by=admin.id,
        )
        run_experiment(session, exp.id)

        out = {
            "tenant_id": str(t1.id),
            "project_id": str(p1.id),
            "agents": [str(a.id) for a in agents],
            "ingest": {
                "accepted": result.accepted,
                "rejected": result.rejected,
                "duplicate": result.duplicate,
            },
            "benchmark_version": imp.version,
            "experiment_id": str(exp.id),
            "gate_decision": exp.gate_decision,
            "experiment_status": exp.status,
        }
        print(json.dumps(out, indent=2))
        return out
