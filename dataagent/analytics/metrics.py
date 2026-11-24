"""Metric registry and daily computation with authorized-agent filtering."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from dataagent.storage import models as m


@dataclass(frozen=True)
class MetricDef:
    name: str
    unit: str
    description: str
    dimensions: tuple[str, ...]
    empty_state: str
    requires_field: str | None = None  # e.g. cost
    module: str | None = None


METRIC_REGISTRY: dict[str, MetricDef] = {
    "session_volume": MetricDef(
        "session_volume",
        "count",
        "Unique production sessions by local date",
        ("state",),
        "no_sessions",
        module="sessions",
    ),
    "completion_rate": MetricDef(
        "completion_rate",
        "rate",
        "Technical completion rate",
        (),
        "no_sessions",
        module="sessions",
    ),
    "success_rate": MetricDef(
        "success_rate",
        "rate",
        "Business success among known terminal outcomes",
        (),
        "insufficient_outcomes",
        module="sessions",
    ),
    "ratings": MetricDef(
        "ratings",
        "score",
        "Latest effective feedback per rater/subject/scale",
        ("scale",),
        "no_ratings",
        module="feedback",
    ),
    "dau": MetricDef(
        "dau", "count", "Daily active pseudonymous users", (), "no_users", module="sessions"
    ),
    "mau": MetricDef(
        "mau",
        "count",
        "Rolling 30-day active pseudonymous users",
        (),
        "no_users",
        module="sessions",
    ),
    "tokens": MetricDef(
        "tokens",
        "tokens",
        "Token usage by class",
        ("usage_class", "token_kind"),
        "no_usage",
        module="cost",
        requires_field=None,
    ),
    "cost": MetricDef(
        "cost",
        "USD",
        "Estimated or known cost",
        ("usage_class",),
        "no_cost_coverage",
        module="cost",
        requires_field="cost",
    ),
    "top_kb": MetricDef(
        "top_kb", "count", "Top KB retrievals/citations", ("component_id",), "empty", module="kb"
    ),
    "top_tools": MetricDef(
        "top_tools", "count", "Top tools", ("component_id",), "empty", module="tools"
    ),
    "top_skills": MetricDef(
        "top_skills", "count", "Top skills", ("component_id",), "empty", module="skills"
    ),
    "latency": MetricDef(
        "latency",
        "ms",
        "End-to-end wall time P50/P95 from runs",
        ("percentile",),
        "no_runs",
        module="activity",
    ),
}


def compute_daily_metrics(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    agent_ids: set[uuid.UUID],
    local_date: date,
    as_of: datetime | None = None,
) -> list[m.DailyMetric]:
    """Recompute daily metrics for authorized agents only."""
    as_of = as_of or datetime.now(UTC)
    out: list[m.DailyMetric] = []
    if not agent_ids:
        return out

    # clear existing for these agents/date
    existing = session.execute(
        select(m.DailyMetric).where(
            m.DailyMetric.tenant_id == tenant_id,
            m.DailyMetric.project_id == project_id,
            m.DailyMetric.local_date == local_date,
            m.DailyMetric.agent_id.in_(list(agent_ids)),
        )
    ).scalars()
    for row in existing:
        session.delete(row)
    session.flush()

    for agent_id in sorted(agent_ids, key=str):
        out.extend(
            _compute_agent_day(session, tenant_id, project_id, agent_id, local_date, as_of)
        )
    session.flush()
    return out


def _upsert(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    agent_id: uuid.UUID,
    metric: str,
    local_date: date,
    dimension_key: str = "",
    dimensions: dict[str, Any] | None = None,
    numerator: Decimal | None = None,
    denominator: Decimal | None = None,
    value: Decimal | None = None,
    known_count: int = 0,
    unknown_count: int = 0,
    as_of: datetime,
    payload: dict[str, Any] | None = None,
) -> m.DailyMetric:
    row = m.DailyMetric(
        tenant_id=tenant_id,
        project_id=project_id,
        agent_id=agent_id,
        metric=metric,
        local_date=local_date,
        dimension_key=dimension_key,
        dimensions=dimensions or {},
        numerator=numerator,
        denominator=denominator,
        value=value,
        known_count=known_count,
        unknown_count=unknown_count,
        as_of=as_of,
        payload=payload or {},
    )
    session.add(row)
    return row


def _compute_agent_day(
    session: Session,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    agent_id: uuid.UUID,
    local_date: date,
    as_of: datetime,
) -> list[m.DailyMetric]:
    rows: list[m.DailyMetric] = []
    sessions = list(
        session.execute(
            select(m.Session).where(
                m.Session.tenant_id == tenant_id,
                m.Session.project_id == project_id,
                m.Session.agent_id == agent_id,
                m.Session.local_date == local_date,
                m.Session.traffic_tag == "production",
            )
        ).scalars()
    )

    by_state: dict[str, int] = {}
    for s in sessions:
        by_state[s.state] = by_state.get(s.state, 0) + 1
    for state, count in sorted(by_state.items()):
        rows.append(
            _upsert(
                session,
                tenant_id=tenant_id,
                project_id=project_id,
                agent_id=agent_id,
                metric="session_volume",
                local_date=local_date,
                dimension_key=state,
                dimensions={"state": state},
                value=Decimal(count),
                known_count=count,
                as_of=as_of,
            )
        )
    rows.append(
        _upsert(
            session,
            tenant_id=tenant_id,
            project_id=project_id,
            agent_id=agent_id,
            metric="session_volume",
            local_date=local_date,
            dimension_key="__total__",
            value=Decimal(len(sessions)),
            known_count=len(sessions),
            as_of=as_of,
        )
    )

    tech_known = [s for s in sessions if s.technical_completed is not None]
    tech_ok = [s for s in tech_known if s.technical_completed]
    rows.append(
        _upsert(
            session,
            tenant_id=tenant_id,
            project_id=project_id,
            agent_id=agent_id,
            metric="completion_rate",
            local_date=local_date,
            numerator=Decimal(len(tech_ok)),
            denominator=Decimal(len(tech_known)) if tech_known else None,
            value=(Decimal(len(tech_ok)) / Decimal(len(tech_known))) if tech_known else None,
            known_count=len(tech_known),
            unknown_count=len(sessions) - len(tech_known),
            as_of=as_of,
            payload={"outcome_coverage": len(tech_known) / len(sessions) if sessions else 0},
        )
    )

    biz_known = [s for s in sessions if s.business_success is not None]
    biz_ok = [s for s in biz_known if s.business_success]
    rows.append(
        _upsert(
            session,
            tenant_id=tenant_id,
            project_id=project_id,
            agent_id=agent_id,
            metric="success_rate",
            local_date=local_date,
            numerator=Decimal(len(biz_ok)),
            denominator=Decimal(len(biz_known)) if biz_known else None,
            value=(Decimal(len(biz_ok)) / Decimal(len(biz_known))) if biz_known else None,
            known_count=len(biz_known),
            unknown_count=len(sessions) - len(biz_known),
            as_of=as_of,
            payload={"outcome_coverage": len(biz_known) / len(sessions) if sessions else 0},
        )
    )

    # DAU
    users = {s.user_pseudonym for s in sessions if s.user_pseudonym}
    rows.append(
        _upsert(
            session,
            tenant_id=tenant_id,
            project_id=project_id,
            agent_id=agent_id,
            metric="dau",
            local_date=local_date,
            value=Decimal(len(users)),
            known_count=len(users),
            as_of=as_of,
        )
    )

    # MAU rolling 30 days
    start = local_date - timedelta(days=29)
    mau_users = set(
        session.execute(
            select(m.Session.user_pseudonym).where(
                m.Session.tenant_id == tenant_id,
                m.Session.project_id == project_id,
                m.Session.agent_id == agent_id,
                m.Session.local_date >= start,
                m.Session.local_date <= local_date,
                m.Session.traffic_tag == "production",
                m.Session.user_pseudonym.is_not(None),
            )
        ).scalars()
    )
    rows.append(
        _upsert(
            session,
            tenant_id=tenant_id,
            project_id=project_id,
            agent_id=agent_id,
            metric="mau",
            local_date=local_date,
            value=Decimal(len(mau_users)),
            known_count=len(mau_users),
            as_of=as_of,
        )
    )

    # Ratings — latest effective per rater/subject/scale
    feedbacks = list(
        session.execute(
            select(m.FeedbackRevision).where(
                m.FeedbackRevision.tenant_id == tenant_id,
                m.FeedbackRevision.project_id == project_id,
                m.FeedbackRevision.agent_id == agent_id,
                func.date(m.FeedbackRevision.effective_at) == local_date,
            )
        ).scalars()
    )
    latest: dict[tuple, m.FeedbackRevision] = {}
    for f in feedbacks:
        key = (f.subject_type, f.subject_id, f.rater_pseudonym, f.scale)
        prev = latest.get(key)
        if prev is None or f.revision > prev.revision or (
            f.revision == prev.revision and f.effective_at > prev.effective_at
        ):
            latest[key] = f
    by_scale: dict[str, list[m.FeedbackRevision]] = {}
    for f in latest.values():
        if f.retracted:
            continue
        by_scale.setdefault(f.scale, []).append(f)
    for scale, items in sorted(by_scale.items()):
        scores = [float(i.score) for i in items if i.score is not None]
        hist: dict[str, int] = {}
        for sc in scores:
            hist[str(sc)] = hist.get(str(sc), 0) + 1
        mean = sum(scores) / len(scores) if scores else None
        rows.append(
            _upsert(
                session,
                tenant_id=tenant_id,
                project_id=project_id,
                agent_id=agent_id,
                metric="ratings",
                local_date=local_date,
                dimension_key=scale,
                dimensions={"scale": scale},
                value=Decimal(str(mean)) if mean is not None else None,
                known_count=len(scores),
                unknown_count=len(items) - len(scores),
                as_of=as_of,
                payload={"histogram": hist, "count": len(items)},
            )
        )

    # Tokens & cost
    usages = list(
        session.execute(
            select(m.ModelUsage).where(
                m.ModelUsage.tenant_id == tenant_id,
                m.ModelUsage.project_id == project_id,
                m.ModelUsage.agent_id == agent_id,
                m.ModelUsage.local_date == local_date,
            )
        ).scalars()
    )
    for uclass in sorted({u.usage_class for u in usages}):
        subset = [u for u in usages if u.usage_class == uclass]
        for kind, attr in (
            ("input", "input_tokens"),
            ("output", "output_tokens"),
            ("cache", "cache_tokens"),
        ):
            total = sum(getattr(u, attr) for u in subset)
            rows.append(
                _upsert(
                    session,
                    tenant_id=tenant_id,
                    project_id=project_id,
                    agent_id=agent_id,
                    metric="tokens",
                    local_date=local_date,
                    dimension_key=f"{uclass}:{kind}",
                    dimensions={"usage_class": uclass, "token_kind": kind},
                    value=Decimal(total),
                    known_count=len(subset),
                    as_of=as_of,
                )
            )
        known_cost = [u for u in subset if u.cost_usd is not None]
        unknown_cost = len(subset) - len(known_cost)
        cost_sum = sum((u.cost_usd for u in known_cost), Decimal("0"))
        rows.append(
            _upsert(
                session,
                tenant_id=tenant_id,
                project_id=project_id,
                agent_id=agent_id,
                metric="cost",
                local_date=local_date,
                dimension_key=uclass,
                dimensions={"usage_class": uclass},
                value=cost_sum,
                known_count=len(known_cost),
                unknown_count=unknown_cost,
                as_of=as_of,
                payload={
                    "estimated_count": sum(1 for u in known_cost if u.cost_estimated),
                    "rate_unknown_count": sum(1 for u in subset if u.rate_unknown),
                },
            )
        )

    # Top KB / tools / skills
    for kind, metric in (("kb", "top_kb"), ("tool", "top_tools"), ("skill", "top_skills")):
        acts = list(
            session.execute(
                select(m.Activity).where(
                    m.Activity.tenant_id == tenant_id,
                    m.Activity.project_id == project_id,
                    m.Activity.agent_id == agent_id,
                    m.Activity.local_date == local_date,
                    m.Activity.kind == kind,
                )
            ).scalars()
        )
        counts: dict[str, dict[str, Any]] = {}
        for a in acts:
            c = counts.setdefault(
                a.component_id, {"calls": 0, "ok": 0, "citations": 0, "sessions": set()}
            )
            c["calls"] += 1
            if a.success:
                c["ok"] += 1
            if a.is_citation:
                c["citations"] += 1
            if a.session_id:
                c["sessions"].add(a.session_id)
        # deterministic tie: sort by -calls, component_id
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1]["calls"], kv[0]))
        for component_id, stats in ranked[:50]:
            rows.append(
                _upsert(
                    session,
                    tenant_id=tenant_id,
                    project_id=project_id,
                    agent_id=agent_id,
                    metric=metric,
                    local_date=local_date,
                    dimension_key=component_id,
                    dimensions={"component_id": component_id},
                    value=Decimal(stats["calls"]),
                    known_count=stats["calls"],
                    as_of=as_of,
                    payload={
                        "distinct_sessions": len(stats["sessions"]),
                        "reliability": (stats["ok"] / stats["calls"]) if stats["calls"] else None,
                        "citations": stats["citations"],
                    },
                )
            )

    # Latency from runs (end-to-end wall) — avoid nested double count by using run wall only
    walls = [
        r.wall_ms
        for r in session.execute(
            select(m.Run).where(
                m.Run.tenant_id == tenant_id,
                m.Run.project_id == project_id,
                m.Run.agent_id == agent_id,
                func.date(m.Run.ended_at) == local_date,
                m.Run.wall_ms.is_not(None),
            )
        ).scalars()
        if r.wall_ms is not None
    ]
    walls.sort()
    if walls:
        def pct(p: float) -> int:
            if not walls:
                return 0
            idx = min(len(walls) - 1, max(0, int(round((p / 100.0) * (len(walls) - 1)))))
            return walls[idx]

        for label, val in (("p50", pct(50)), ("p95", pct(95))):
            rows.append(
                _upsert(
                    session,
                    tenant_id=tenant_id,
                    project_id=project_id,
                    agent_id=agent_id,
                    metric="latency",
                    local_date=local_date,
                    dimension_key=label,
                    dimensions={"percentile": label},
                    value=Decimal(val),
                    known_count=len(walls),
                    as_of=as_of,
                )
            )
    return rows


def query_metrics(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    authorized_agent_ids: set[uuid.UUID],
    metric: str,
    start: date,
    end: date,
    top_n: int | None = None,
) -> dict[str, Any]:
    """Query precomputed metrics filtered to authorized agents; recompute scope aggregates."""
    defn = METRIC_REGISTRY.get(metric)
    if defn is None:
        return {"error": "unknown_metric", "empty_state": "unavailable"}
    if not authorized_agent_ids:
        return {
            "metric": metric,
            "unit": defn.unit,
            "rows": [],
            "empty_state": defn.empty_state,
            "freshness": None,
        }

    rows = list(
        session.execute(
            select(m.DailyMetric).where(
                m.DailyMetric.tenant_id == tenant_id,
                m.DailyMetric.project_id == project_id,
                m.DailyMetric.metric == metric,
                m.DailyMetric.local_date >= start,
                m.DailyMetric.local_date <= end,
                m.DailyMetric.agent_id.in_(list(authorized_agent_ids)),
            )
        ).scalars()
    )

    # For DAU/MAU across agents — re-deduplicate from source when multi-agent
    if metric in ("dau", "mau") and len(authorized_agent_ids) > 1:
        return _cross_agent_distinct_users(
            session, tenant_id, project_id, authorized_agent_ids, metric, start, end, defn
        )

    # Aggregate across authorized agents by date + dimension
    bucket: dict[tuple, dict[str, Any]] = {}
    freshness = None
    for r in rows:
        key = (r.local_date.isoformat(), r.dimension_key)
        b = bucket.setdefault(
            key,
            {
                "local_date": r.local_date.isoformat(),
                "dimension_key": r.dimension_key,
                "dimensions": r.dimensions,
                "value": Decimal("0"),
                "numerator": Decimal("0"),
                "denominator": Decimal("0"),
                "known_count": 0,
                "unknown_count": 0,
                "payload": {},
            },
        )
        if r.value is not None:
            b["value"] += r.value
        if r.numerator is not None:
            b["numerator"] += r.numerator
        if r.denominator is not None:
            b["denominator"] += r.denominator
        b["known_count"] += r.known_count
        b["unknown_count"] += r.unknown_count
        if freshness is None or r.as_of > freshness:
            freshness = r.as_of

    out_rows = []
    for _, b in sorted(bucket.items()):
        if b["denominator"]:
            b["value"] = b["numerator"] / b["denominator"]
        b["value"] = float(b["value"]) if b["value"] is not None else None
        b["numerator"] = float(b["numerator"]) if b["numerator"] else None
        b["denominator"] = float(b["denominator"]) if b["denominator"] else None
        out_rows.append(b)

    if metric.startswith("top_") and top_n:
        # re-rank across agents
        by_comp: dict[str, dict[str, Any]] = {}
        for r in out_rows:
            cid = r["dimension_key"]
            if cid == "__total__":
                continue
            cur = by_comp.get(cid)
            if cur is None or (r["value"] or 0) > (cur["value"] or 0):
                by_comp[cid] = r
            elif cur and (r["value"] or 0) == (cur["value"] or 0):
                # deterministic tie — keep lexicographically smaller already sorted
                if cid < cur["dimension_key"]:
                    by_comp[cid] = r
        out_rows = sorted(
            by_comp.values(), key=lambda x: (-(x["value"] or 0), x["dimension_key"])
        )[:top_n]

    return {
        "metric": metric,
        "unit": defn.unit,
        "rows": out_rows,
        "empty_state": defn.empty_state if not out_rows else None,
        "freshness": freshness.isoformat() if freshness else None,
    }


def _cross_agent_distinct_users(
    session: Session,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    agent_ids: set[uuid.UUID],
    metric: str,
    start: date,
    end: date,
    defn: MetricDef,
) -> dict[str, Any]:
    out_rows = []
    d = start
    while d <= end:
        if metric == "dau":
            users = set(
                session.execute(
                    select(m.Session.user_pseudonym).where(
                        m.Session.tenant_id == tenant_id,
                        m.Session.project_id == project_id,
                        m.Session.agent_id.in_(list(agent_ids)),
                        m.Session.local_date == d,
                        m.Session.traffic_tag == "production",
                        m.Session.user_pseudonym.is_not(None),
                    )
                ).scalars()
            )
        else:
            window_start = d - timedelta(days=29)
            users = set(
                session.execute(
                    select(m.Session.user_pseudonym).where(
                        m.Session.tenant_id == tenant_id,
                        m.Session.project_id == project_id,
                        m.Session.agent_id.in_(list(agent_ids)),
                        m.Session.local_date >= window_start,
                        m.Session.local_date <= d,
                        m.Session.traffic_tag == "production",
                        m.Session.user_pseudonym.is_not(None),
                    )
                ).scalars()
            )
        out_rows.append(
            {
                "local_date": d.isoformat(),
                "dimension_key": "",
                "dimensions": {},
                "value": float(len(users)),
                "known_count": len(users),
                "unknown_count": 0,
            }
        )
        d += timedelta(days=1)
    return {
        "metric": metric,
        "unit": defn.unit,
        "rows": out_rows,
        "empty_state": defn.empty_state if not any(r["value"] for r in out_rows) else None,
        "freshness": datetime.now(UTC).isoformat(),
    }
