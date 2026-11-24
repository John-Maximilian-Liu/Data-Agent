"""Experiment lifecycle — snapshot config, execute pairs, score, gate, report."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from dataagent.experiments.scoring import EVALUATORS, EvalInput
from dataagent.providers.azure import AzureOpenAIProvider, get_provider
from dataagent.storage import models as m

AgentCaller = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


class FakeAgentCaller:
    """Offline fake agent — plants regressions when release version contains 'bad'."""

    def __call__(self, release: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
        version = release.get("version") or ""
        expected = case.get("expected_answer") or ""
        tools = case.get("permitted_tools") or []
        if "bad" in version or "regress" in version:
            return {
                "answer": "WRONG",
                "tool_calls": [{"name": "evil_tool", "effect": "boom"}] if tools else [],
                "skills": [],
                "cited_ids": [],
                "steps": None,  # missing traces
                "wall_ms": 5000,
                "cost_usd": 0.05,
                "status": "completed",
                "business_success": False,
            }
        return {
            "answer": expected,
            "tool_calls": [{"name": tools[0], "effect": "ok"}] if tools else [],
            "skills": [{"name": s} for s in (case.get("permitted_skills") or [])],
            "cited_ids": list(case.get("document_labels") or []),
            "retrieval_ids": list(case.get("document_labels") or []),
            "steps": [{"name": "plan"}, {"name": "act"}],
            "wall_ms": 800,
            "cost_usd": 0.01,
            "status": "completed",
            "business_success": True,
            "k": 5,
        }


def start_experiment(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    agent_id: uuid.UUID,
    benchmark_version_id: uuid.UUID,
    baseline_release_id: uuid.UUID,
    candidate_release_id: uuid.UUID,
    created_by: uuid.UUID,
    suites: list[str] | None = None,
    evaluators: list[str] | None = None,
    config: dict[str, Any] | None = None,
) -> m.Experiment:
    suites = suites or ["fixed_core", "regression"]
    evaluators = evaluators or list(EVALUATORS.keys())
    # snapshot case revisions
    members = list(
        session.execute(
            select(m.SuiteMembership).where(
                m.SuiteMembership.benchmark_version_id == benchmark_version_id,
                m.SuiteMembership.suite.in_(suites),
            )
        ).scalars()
    )
    case_ids = sorted({str(m_.case_revision_id) for m_ in members})
    baseline = session.get(m.Release, baseline_release_id)
    candidate = session.get(m.Release, candidate_release_id)
    snapshot = {
        "case_revision_ids": case_ids,
        "suites": suites,
        "evaluators": evaluators,
        "baseline": {
            "id": str(baseline_release_id),
            "version": baseline.version if baseline else None,
            "artifacts": baseline.artifact_versions if baseline else {},
        },
        "candidate": {
            "id": str(candidate_release_id),
            "version": candidate.version if candidate else None,
            "artifacts": candidate.artifact_versions if candidate else {},
        },
        "config": config or {"repetitions": 1, "seed": 42, "concurrency": 4},
        "comparability": {},
    }
    exp = m.Experiment(
        tenant_id=tenant_id,
        project_id=project_id,
        agent_id=agent_id,
        benchmark_version_id=benchmark_version_id,
        baseline_release_id=baseline_release_id,
        candidate_release_id=candidate_release_id,
        status="queued",
        config_snapshot=snapshot,
        suites=suites,
        evaluators=evaluators,
        created_by=created_by,
    )
    session.add(exp)
    session.flush()
    return exp


def run_experiment(
    session: Session,
    experiment_id: uuid.UUID,
    *,
    caller: AgentCaller | None = None,
    provider: AzureOpenAIProvider | None = None,
    budget_usd: float = 50.0,
) -> m.Experiment:
    exp = session.get(m.Experiment, experiment_id)
    if exp is None:
        raise LookupError("experiment_not_found")
    if exp.status == "canceled":
        return exp

    caller = caller or FakeAgentCaller()
    provider = provider or get_provider()
    provider.budget_remaining = budget_usd
    exp.status = "running"
    session.flush()

    snap = exp.config_snapshot or {}
    case_ids = [uuid.UUID(x) for x in snap.get("case_revision_ids") or []]
    reps = int((snap.get("config") or {}).get("repetitions") or 1)
    baseline_rel = session.get(m.Release, exp.baseline_release_id)
    candidate_rel = session.get(m.Release, exp.candidate_release_id)
    baseline_info = {
        "version": baseline_rel.version if baseline_rel else "baseline",
        "id": str(exp.baseline_release_id),
    }
    candidate_info = {
        "version": candidate_rel.version if candidate_rel else "candidate",
        "id": str(exp.candidate_release_id),
    }

    completed_pairs = 0
    dropped: list[dict[str, Any]] = []
    agent_failures = 0
    infra_failures = 0

    for case_id in case_ids:
        # resume support: skip if both variants already scored
        exp_refresh = session.get(m.Experiment, experiment_id)
        if exp_refresh is None or exp_refresh.status == "canceled":
            break

        case = session.get(m.CaseRevision, case_id)
        if case is None:
            dropped.append({"case": str(case_id), "reason": "missing_case"})
            continue
        case_dict = {
            "id": str(case.id),
            "question": case.question,
            "expected_answer": case.expected_answer,
            "document_labels": case.document_labels or [],
            "permitted_tools": case.permitted_tools or [],
            "permitted_skills": case.permitted_skills or [],
            "fixtures": case.fixtures or {},
            "rubric": case.rubric,
            "task_category": case.task_category,
            "holdout": case.holdout,
        }

        for variant, rel in (("baseline", baseline_info), ("candidate", candidate_info)):
            for rep in range(reps):
                if not provider.reserve_budget(0.01):
                    exp.status = "budget_exhausted"
                    session.flush()
                    report = _build_report(session, exp, dropped, completed_pairs)
                    exp.report = report["structured"]
                    exp.gate_decision = report["gate_decision"]
                    return exp

                existing = session.execute(
                    select(m.Execution).where(
                        m.Execution.experiment_id == exp.id,
                        m.Execution.case_revision_id == case_id,
                        m.Execution.variant == variant,
                        m.Execution.replicate == rep,
                    )
                ).scalar_one_or_none()
                if existing and existing.status == "completed":
                    continue

                started = datetime.now(UTC)
                try:
                    output = caller(rel, case_dict)
                    failure_kind = None
                    status = "completed"
                    if output.get("status") == "error":
                        failure_kind = "agent"
                        status = "failed"
                        agent_failures += 1
                except TimeoutError as e:
                    output = {"error": str(e)}
                    failure_kind = "transport"
                    status = "failed"
                    infra_failures += 1
                except Exception as e:
                    output = {"error": str(e)}
                    failure_kind = "infrastructure"
                    status = "failed"
                    infra_failures += 1

                ended = datetime.now(UTC)
                execution = existing or m.Execution(
                    tenant_id=exp.tenant_id,
                    project_id=exp.project_id,
                    experiment_id=exp.id,
                    case_revision_id=case_id,
                    variant=variant,
                    replicate=rep,
                )
                execution.status = status
                execution.failure_kind = failure_kind
                execution.output = output
                execution.started_at = started
                execution.ended_at = ended
                execution.wall_ms = output.get("wall_ms")
                execution.usage = {
                    "cost_usd": output.get("cost_usd"),
                    "cost_uncertain": output.get("cost_usd") is None,
                }
                if existing is None:
                    session.add(execution)
                session.flush()
                provider.settle(0.01, float(output["cost_usd"]) if output.get("cost_usd") is not None else None)

                if status != "completed":
                    continue

                observations = {
                    "tool_calls": output.get("tool_calls"),
                    "skills": output.get("skills"),
                    "cited_ids": output.get("cited_ids"),
                    "retrieval_ids": output.get("retrieval_ids"),
                    "steps": output.get("steps"),
                    "wall_ms": output.get("wall_ms"),
                    "cost_usd": output.get("cost_usd"),
                }
                for ev_name in exp.evaluators or []:
                    ev = EVALUATORS.get(ev_name)
                    if ev is None:
                        continue
                    result = ev.evaluate(
                        EvalInput(
                            case=case_dict, output=output, variant=variant, observations=observations
                        ),
                        provider=provider,
                    )
                    score_row = session.execute(
                        select(m.Score).where(
                            m.Score.experiment_id == exp.id,
                            m.Score.case_revision_id == case_id,
                            m.Score.variant == variant,
                            m.Score.replicate == rep,
                            m.Score.evaluator == result.evaluator,
                        )
                    ).scalar_one_or_none()
                    if score_row is None:
                        score_row = m.Score(
                            tenant_id=exp.tenant_id,
                            project_id=exp.project_id,
                            experiment_id=exp.id,
                            execution_id=execution.id,
                            case_revision_id=case_id,
                            variant=variant,
                            replicate=rep,
                            evaluator=result.evaluator,
                            evaluator_version=result.version,
                        )
                        session.add(score_row)
                    score_row.value = result.value
                    score_row.missing = result.missing
                    score_row.explanation = result.explanation
                    score_row.evidence_ids = result.evidence_ids or []
                    score_row.metadata_ = result.metadata or {}
                session.flush()

        completed_pairs += 1
        # checkpoint progress
        exp.checkpoint = {"completed_cases": completed_pairs, "last_case": str(case_id)}  # type: ignore[attr-defined]
        # store on config_snapshot progress key
        snap = dict(exp.config_snapshot or {})
        snap["progress"] = {"completed_cases": completed_pairs, "last_case": str(case_id)}
        exp.config_snapshot = snap
        session.flush()

    if exp.status == "budget_exhausted":
        pass
    elif exp.status == "canceled":
        pass
    elif agent_failures or infra_failures:
        exp.status = "partial_failed"
    else:
        exp.status = "completed"

    report = _build_report(session, exp, dropped, completed_pairs)
    exp.report = report["structured"]
    exp.gate_decision = report["gate_decision"]
    exp.completed_at = datetime.now(UTC)

    # persist Report row
    existing_report = session.execute(
        select(m.Report).where(m.Report.experiment_id == exp.id)
    ).scalar_one_or_none()
    if existing_report is None:
        session.add(
            m.Report(
                tenant_id=exp.tenant_id,
                project_id=exp.project_id,
                experiment_id=exp.id,
                structured=report["structured"],
                narrative=report.get("narrative"),
                gate_decision=report["gate_decision"],
            )
        )
    else:
        existing_report.structured = report["structured"]
        existing_report.narrative = report.get("narrative")
        existing_report.gate_decision = report["gate_decision"]
    session.flush()
    return exp


def cancel_experiment(session: Session, experiment_id: uuid.UUID) -> m.Experiment:
    exp = session.get(m.Experiment, experiment_id)
    if exp is None:
        raise LookupError("experiment_not_found")
    if exp.status in ("completed", "partial_failed", "budget_exhausted"):
        return exp
    exp.status = "canceled"
    exp.completed_at = datetime.now(UTC)
    session.flush()
    return exp


def _build_report(
    session: Session, exp: m.Experiment, dropped: list[dict[str, Any]], completed_pairs: int
) -> dict[str, Any]:
    scores = list(
        session.execute(select(m.Score).where(m.Score.experiment_id == exp.id)).scalars()
    )
    by_eval: dict[str, dict[str, list[Decimal]]] = {}
    regressions: list[dict[str, Any]] = []

    case_variants: dict[tuple[uuid.UUID, str], dict[str, m.Score]] = {}
    for s in scores:
        if s.missing:
            if s.evaluator in ("task_success", "tools", "skills"):
                # missing is not zero
                continue
            continue
        by_eval.setdefault(s.evaluator, {}).setdefault(s.variant, []).append(s.value or Decimal("0"))
        case_variants.setdefault((s.case_revision_id, s.evaluator), {})[s.variant] = s

    for (case_id, evaluator), variants in case_variants.items():
        b = variants.get("baseline")
        c = variants.get("candidate")
        if b and c and b.value is not None and c.value is not None:
            if float(c.value) + 1e-9 < float(b.value) and evaluator in (
                "task_success",
                "tools",
                "skills",
                "rag",
            ):
                regressions.append(
                    {
                        "case_revision_id": str(case_id),
                        "evaluator": evaluator,
                        "baseline": float(b.value),
                        "candidate": float(c.value),
                        "component": c.explanation,
                    }
                )

    dimension_breakdown = {}
    for ev, variants in by_eval.items():
        dimension_breakdown[ev] = {
            variant: {
                "mean": float(sum(vals) / len(vals)) if vals else None,
                "n": len(vals),
            }
            for variant, vals in variants.items()
        }

    # gates — quality/reliability/latency/cost independent
    gates = {
        "quality": _gate_quality(dimension_breakdown, regressions),
        "reliability": _gate_reliability(session, exp),
        "latency": _gate_latency(dimension_breakdown),
        "cost": _gate_cost(session, exp),
    }
    decisions = [g["decision"] for g in gates.values()]
    if "fail" in decisions:
        overall = "fail"
    elif "inconclusive" in decisions or completed_pairs == 0:
        overall = "inconclusive"
    else:
        overall = "pass"

    structured = {
        "experiment_id": str(exp.id),
        "status": exp.status,
        "suite_coverage": exp.suites,
        "completed_pairs": completed_pairs,
        "dropped_pairs": dropped,
        "dimension_breakdown": dimension_breakdown,
        "critical_regressions": regressions,
        "gates": gates,
        "gate_decision": overall,
        "missing_signals": [
            s.evaluator
            for s in scores
            if s.missing
        ],
    }
    # narrative independent of gates
    narrative = None
    try:
        provider = get_provider()
        narrative = provider.narrative(structured)
    except Exception:
        narrative = None

    return {"structured": structured, "gate_decision": overall, "narrative": narrative}


def _gate_quality(breakdown: dict, regressions: list) -> dict[str, Any]:
    ts = breakdown.get("task_success") or {}
    if not ts.get("baseline") or not ts.get("candidate"):
        return {"decision": "inconclusive", "reason": "insufficient_pairs", "denominator": 0}
    if regressions:
        return {
            "decision": "fail",
            "reason": "critical_regression",
            "count": len(regressions),
            "denominator": ts["candidate"]["n"],
        }
    return {"decision": "pass", "denominator": ts["candidate"]["n"]}


def _gate_reliability(session: Session, exp: m.Experiment) -> dict[str, Any]:
    execs = list(
        session.execute(select(m.Execution).where(m.Execution.experiment_id == exp.id)).scalars()
    )
    if not execs:
        return {"decision": "inconclusive", "reason": "no_executions"}
    fails = [e for e in execs if e.status != "completed"]
    rate = len(fails) / len(execs)
    return {
        "decision": "fail" if rate > 0.2 else "pass",
        "failure_rate": rate,
        "denominator": len(execs),
    }


def _gate_latency(breakdown: dict) -> dict[str, Any]:
    rt = breakdown.get("runtime_cost") or {}
    b = (rt.get("baseline") or {}).get("mean")
    c = (rt.get("candidate") or {}).get("mean")
    if b is None or c is None:
        return {"decision": "inconclusive", "reason": "missing_latency"}
    # fail if candidate > 2x baseline
    return {
        "decision": "fail" if c > 2 * b else "pass",
        "baseline_ms": b,
        "candidate_ms": c,
    }


def _gate_cost(session: Session, exp: m.Experiment) -> dict[str, Any]:
    execs = list(
        session.execute(select(m.Execution).where(m.Execution.experiment_id == exp.id)).scalars()
    )
    costs = []
    uncertain = 0
    for e in execs:
        u = e.usage or {}
        if u.get("cost_uncertain"):
            uncertain += 1
        elif u.get("cost_usd") is not None:
            costs.append(float(u["cost_usd"]))
    if not costs and uncertain:
        return {"decision": "inconclusive", "reason": "cost_uncertainty", "uncertain": uncertain}
    if not costs:
        return {"decision": "inconclusive", "reason": "no_cost"}
    return {"decision": "pass", "mean_cost": sum(costs) / len(costs), "uncertain": uncertain}


def submit_ci_result(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    experiment_id: uuid.UUID,
    case_revision_id: uuid.UUID,
    variant: str,
    replicate: int,
    submission_key: str,
    output: dict[str, Any],
) -> m.Execution:
    """Private CI submission — deduplicated by submission_key / execution key."""
    existing = session.execute(
        select(m.Execution).where(
            m.Execution.experiment_id == experiment_id,
            m.Execution.case_revision_id == case_revision_id,
            m.Execution.variant == variant,
            m.Execution.replicate == replicate,
        )
    ).scalar_one_or_none()
    if existing and existing.submission_key == submission_key:
        return existing
    if existing and existing.submission_key and existing.submission_key != submission_key:
        # replay with same logical key but different — treat as idempotent replace only if not completed
        if existing.status == "completed":
            return existing

    row = existing or m.Execution(
        tenant_id=tenant_id,
        project_id=project_id,
        experiment_id=experiment_id,
        case_revision_id=case_revision_id,
        variant=variant,
        replicate=replicate,
    )
    row.submission_key = submission_key
    row.output = output
    row.status = "completed"
    row.started_at = datetime.now(UTC)
    row.ended_at = datetime.now(UTC)
    row.wall_ms = output.get("wall_ms")
    row.usage = {"cost_usd": output.get("cost_usd")}
    if existing is None:
        session.add(row)
    session.flush()
    return row
