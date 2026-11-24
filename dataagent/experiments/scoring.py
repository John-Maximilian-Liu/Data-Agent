"""Typed evaluator registry — deterministic rules + optional Azure judge."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from dataagent.providers.azure import AzureOpenAIProvider


@dataclass
class EvalInput:
    case: dict[str, Any]
    output: dict[str, Any]
    variant: str
    observations: dict[str, Any]


@dataclass
class EvalResult:
    evaluator: str
    version: str
    value: Decimal | None
    missing: bool = False
    explanation: str = ""
    evidence_ids: list[str] | None = None
    metadata: dict[str, Any] | None = None


class Evaluator(Protocol):
    name: str
    version: str

    def evaluate(self, inp: EvalInput, provider: AzureOpenAIProvider | None = None) -> EvalResult: ...


class TaskSuccessEvaluator:
    name = "task_success"
    version = "1.0"

    def evaluate(self, inp: EvalInput, provider: AzureOpenAIProvider | None = None) -> EvalResult:
        expected = inp.case.get("expected_answer")
        answer = inp.output.get("answer")
        if expected is None:
            return EvalResult(self.name, self.version, None, missing=True, explanation="no expected")
        if answer is None:
            return EvalResult(self.name, self.version, None, missing=True, explanation="no answer")
        # structured / exact
        if str(answer).strip() == str(expected).strip():
            return EvalResult(self.name, self.version, Decimal("1"), explanation="exact match")
        # business outcome rule
        if inp.output.get("business_success") is True and inp.case.get("task_category") == "outcome":
            return EvalResult(self.name, self.version, Decimal("1"), explanation="business outcome")
        # open answer — Azure rubric if available
        if provider is not None:
            judged = provider.judge(
                rubric=(inp.case.get("rubric") or {}).get("text")
                if isinstance(inp.case.get("rubric"), dict)
                else "Grade semantic match 0-1",
                question=str(inp.case.get("question") or ""),
                answer=str(answer),
                expected=str(expected),
            )
            return EvalResult(
                self.name,
                self.version,
                Decimal(str(judged.get("score", 0))),
                explanation=str(judged.get("explanation") or ""),
                evidence_ids=list(judged.get("evidence_ids") or []),
                metadata={"model": judged.get("model"), "request_id": judged.get("request_id")},
            )
        return EvalResult(self.name, self.version, Decimal("0"), explanation="no match")


class RAGEvaluator:
    name = "rag"
    version = "1.0"

    def evaluate(self, inp: EvalInput, provider: AzureOpenAIProvider | None = None) -> EvalResult:
        refs = set(inp.case.get("document_labels") or [])
        if not refs:
            return EvalResult(self.name, self.version, None, missing=True, explanation="no references")
        cited = set(inp.output.get("cited_ids") or inp.observations.get("cited_ids") or [])
        if not cited and not inp.observations.get("retrieval_ids"):
            return EvalResult(self.name, self.version, None, missing=True, explanation="no retrieval evidence")
        retrieved = set(inp.observations.get("retrieval_ids") or [])
        # Recall@K against references among retrieved
        k = int(inp.output.get("k") or 5)
        top = list(retrieved)[:k]
        hits = len(refs & set(top))
        recall = Decimal(hits) / Decimal(len(refs)) if refs else Decimal("0")
        citation_ok = len(cited & refs) / len(cited) if cited else None
        return EvalResult(
            self.name,
            self.version,
            recall,
            explanation=f"recall@{k}={recall}",
            metadata={"citation_precision": citation_ok},
        )


class ToolsEvaluator:
    name = "tools"
    version = "1.0"

    def evaluate(self, inp: EvalInput, provider: AzureOpenAIProvider | None = None) -> EvalResult:
        permitted = set(inp.case.get("permitted_tools") or [])
        used = inp.observations.get("tool_calls") or inp.output.get("tool_calls") or []
        if not permitted and not used:
            return EvalResult(self.name, self.version, None, missing=True, explanation="no tool signal")
        if not used:
            return EvalResult(self.name, self.version, Decimal("0"), explanation="expected tools unused")
        names = [t.get("name") if isinstance(t, dict) else str(t) for t in used]
        if permitted and not set(names) <= permitted:
            return EvalResult(
                self.name, self.version, Decimal("0"), explanation="unapproved tool selection"
            )
        # schema / expected effects via fixture
        fixture = (inp.case.get("fixtures") or {}).get("tool_effects") or {}
        for t in used:
            if not isinstance(t, dict):
                continue
            expected_effect = fixture.get(t.get("name"))
            if expected_effect is not None and t.get("effect") != expected_effect:
                # allow valid alternative paths listed in fixture
                alts = (inp.case.get("fixtures") or {}).get("alt_paths") or []
                if names not in alts and set(names) not in [set(a) for a in alts]:
                    return EvalResult(
                        self.name, self.version, Decimal("0"), explanation="effect mismatch"
                    )
        return EvalResult(self.name, self.version, Decimal("1"), explanation="tools ok")


class SkillsEvaluator:
    name = "skills"
    version = "1.0"

    def evaluate(self, inp: EvalInput, provider: AzureOpenAIProvider | None = None) -> EvalResult:
        expected = set(inp.case.get("permitted_skills") or [])
        used = inp.observations.get("skills") or inp.output.get("skills") or []
        if not expected and not used:
            return EvalResult(self.name, self.version, None, missing=True, explanation="no skill signal")
        names = [s.get("name") if isinstance(s, dict) else str(s) for s in used]
        if expected and set(names) != expected and not set(names) <= expected:
            return EvalResult(self.name, self.version, Decimal("0"), explanation="skill selection")
        return EvalResult(
            self.name,
            self.version,
            Decimal("1") if used else Decimal("0"),
            explanation="skills coverage",
            metadata={"coverage": len(names) / len(expected) if expected else None},
        )


class PathEvaluator:
    name = "observable_path"
    version = "1.0"

    def evaluate(self, inp: EvalInput, provider: AzureOpenAIProvider | None = None) -> EvalResult:
        steps = inp.observations.get("steps") or inp.output.get("steps")
        if steps is None:
            # missing traces must not become zero
            return EvalResult(self.name, self.version, None, missing=True, explanation="missing traces")
        constraints = (inp.case.get("fixtures") or {}).get("ordering") or []
        names = [s.get("name") if isinstance(s, dict) else str(s) for s in steps]
        for a, b in constraints:
            if a in names and b in names and names.index(a) > names.index(b):
                return EvalResult(self.name, self.version, Decimal("0"), explanation="order violated")
        return EvalResult(self.name, self.version, Decimal("1"), explanation="path ok")


class PromptEvaluator:
    name = "prompt_behavior"
    version = "1.0"

    def evaluate(self, inp: EvalInput, provider: AzureOpenAIProvider | None = None) -> EvalResult:
        contract = (inp.case.get("fixtures") or {}).get("output_contract")
        answer = inp.output.get("answer")
        if contract is None:
            return EvalResult(self.name, self.version, None, missing=True, explanation="no contract")
        if answer is None:
            return EvalResult(self.name, self.version, None, missing=True, explanation="no answer")
        if contract.get("type") == "json":
            try:
                json.loads(answer if isinstance(answer, str) else json.dumps(answer))
                return EvalResult(self.name, self.version, Decimal("1"), explanation="json ok")
            except Exception:
                return EvalResult(self.name, self.version, Decimal("0"), explanation="json fail")
        prefix = contract.get("prefix")
        if prefix and not str(answer).startswith(prefix):
            return EvalResult(self.name, self.version, Decimal("0"), explanation="prefix fail")
        return EvalResult(self.name, self.version, Decimal("1"), explanation="contract ok")


class RuntimeCostEvaluator:
    name = "runtime_cost"
    version = "1.0"

    def evaluate(self, inp: EvalInput, provider: AzureOpenAIProvider | None = None) -> EvalResult:
        wall = inp.output.get("wall_ms") or inp.observations.get("wall_ms")
        cost = inp.output.get("cost_usd") or inp.observations.get("cost_usd")
        if wall is None and cost is None:
            return EvalResult(self.name, self.version, None, missing=True, explanation="no runtime")
        # value is normalized: lower latency better — store wall_ms as value for gates
        return EvalResult(
            self.name,
            self.version,
            Decimal(str(wall)) if wall is not None else None,
            explanation="runtime",
            metadata={"cost_usd": cost, "failures": inp.output.get("error_code")},
        )


EVALUATORS: dict[str, Evaluator] = {
    e.name: e
    for e in (
        TaskSuccessEvaluator(),
        RAGEvaluator(),
        ToolsEvaluator(),
        SkillsEvaluator(),
        PathEvaluator(),
        PromptEvaluator(),
        RuntimeCostEvaluator(),
    )
}
