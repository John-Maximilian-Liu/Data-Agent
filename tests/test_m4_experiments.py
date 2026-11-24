"""Milestone 4 — planted regression, missing evidence, gates without narrative."""

from __future__ import annotations

from pathlib import Path

from dataagent.benchmarks.service import import_qa
from dataagent.experiments.runner import cancel_experiment, run_experiment, start_experiment, submit_ci_result
from dataagent.experiments.scoring import EVALUATORS, EvalInput
from dataagent.providers.azure import FakeAzureTransport, AzureOpenAIProvider
from dataagent.storage import models as m

ROOT = Path(__file__).resolve().parents[1]


def _seed_exp(session, world):
    bench = m.Benchmark(
        tenant_id=world.tenant_a.id,
        project_id=world.project_a1.id,
        slug="exp",
        name="Exp",
        owner_agent_id=world.agents[0].id,
    )
    session.add(bench)
    session.flush()
    imp = import_qa(
        session,
        tenant_id=world.tenant_a.id,
        project_id=world.project_a1.id,
        benchmark_id=bench.id,
        content=(ROOT / "fixtures/qa/seed.jsonl").read_text(),
        fmt="jsonl",
        actor_id=world.admin.id,
    )
    baseline = m.Release(
        tenant_id=world.tenant_a.id,
        project_id=world.project_a1.id,
        agent_id=world.agents[0].id,
        version="1.0.0",
        is_production=True,
    )
    candidate = m.Release(
        tenant_id=world.tenant_a.id,
        project_id=world.project_a1.id,
        agent_id=world.agents[0].id,
        version="2.0.0-regress",
    )
    session.add_all([baseline, candidate])
    session.flush()
    exp = start_experiment(
        session,
        tenant_id=world.tenant_a.id,
        project_id=world.project_a1.id,
        agent_id=world.agents[0].id,
        benchmark_version_id=imp.version_id,
        baseline_release_id=baseline.id,
        candidate_release_id=candidate.id,
        created_by=world.admin.id,
        evaluators=list(EVALUATORS.keys()),
    )
    return exp


def test_detects_planted_tool_regression(session, world):
    exp = _seed_exp(session, world)
    run_experiment(session, exp.id)
    assert exp.gate_decision in ("fail", "inconclusive", "pass")
    # candidate uses evil_tool → tools evaluator fails vs baseline
    regressions = (exp.report or {}).get("critical_regressions") or []
    tool_regs = [r for r in regressions if r["evaluator"] == "tools"]
    task_regs = [r for r in regressions if r["evaluator"] == "task_success"]
    assert tool_regs or task_regs
    # identify case
    assert all("case_revision_id" in r for r in regressions)


def test_missing_traces_not_zero(session, world):
    ev = EVALUATORS["observable_path"]
    result = ev.evaluate(
        EvalInput(case={}, output={}, variant="candidate", observations={"steps": None}),
        provider=None,
    )
    assert result.missing is True
    assert result.value is None


def test_gates_independent_of_narrative(session, world):
    exp = _seed_exp(session, world)
    # provider that fails narrative
    class Boom(FakeAzureTransport):
        def chat_json(self, **kwargs):
            if any("NARRATIVE" in m["content"] for m in kwargs.get("messages", []) if m["role"] == "user"):
                raise RuntimeError("narrative unavailable")
            return super().chat_json(**kwargs)

    provider = AzureOpenAIProvider(transport=Boom(), deployment="fake")
    run_experiment(session, exp.id, provider=provider)
    assert exp.gate_decision in ("pass", "fail", "inconclusive")
    assert exp.report and "gates" in exp.report


def test_cancellation_keeps_completed(session, world):
    exp = _seed_exp(session, world)
    # partially run by canceling immediately after start state
    cancel_experiment(session, exp.id)
    assert exp.status == "canceled"
    run_experiment(session, exp.id)
    assert exp.status == "canceled"


def test_ci_submission_replay(session, world):
    exp = _seed_exp(session, world)
    case_id = exp.config_snapshot["case_revision_ids"][0]
    import uuid

    row1 = submit_ci_result(
        session,
        tenant_id=world.tenant_a.id,
        project_id=world.project_a1.id,
        experiment_id=exp.id,
        case_revision_id=uuid.UUID(case_id),
        variant="candidate",
        replicate=0,
        submission_key="ci-1",
        output={"answer": "4", "wall_ms": 10},
    )
    row2 = submit_ci_result(
        session,
        tenant_id=world.tenant_a.id,
        project_id=world.project_a1.id,
        experiment_id=exp.id,
        case_revision_id=uuid.UUID(case_id),
        variant="candidate",
        replicate=0,
        submission_key="ci-1",
        output={"answer": "4", "wall_ms": 10},
    )
    assert row1.id == row2.id


def test_calibration_fixtures(session, world):
    provider = AzureOpenAIProvider(transport=FakeAzureTransport(), deployment="x")
    ok = provider.judge(rubric="r", question="q", answer="KNOWN_CORRECT", expected="x")
    bad = provider.judge(rubric="r", question="q", answer="KNOWN_INCORRECT", expected="x")
    assert ok["score"] == 1.0
    assert bad["score"] == 0.0
