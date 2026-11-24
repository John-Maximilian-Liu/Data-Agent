"""Benchmark QA import, refresh sampling, candidate review, version publish."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from dataagent.storage import models as m
from dataagent.util.hashing import content_hash


@dataclass
class ImportIssue:
    row: int
    field: str
    code: str
    message: str


@dataclass
class ImportResult:
    valid: bool
    issues: list[ImportIssue] = field(default_factory=list)
    case_count: int = 0
    version_id: uuid.UUID | None = None
    version: int | None = None
    artifact_hash: str | None = None


def _parse_qa_rows(content: str, fmt: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if fmt == "jsonl":
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    elif fmt == "csv":
        reader = csv.DictReader(io.StringIO(content))
        for r in reader:
            rows.append(dict(r))
    else:
        raise ValueError("fmt must be csv or jsonl")
    return rows


def validate_qa_rows(rows: list[dict[str, Any]]) -> list[ImportIssue]:
    issues: list[ImportIssue] = []
    seen_ext: set[str] = set()
    for i, row in enumerate(rows, start=1):
        q = row.get("question")
        a = row.get("expected_answer")
        if not q or not str(q).strip():
            issues.append(ImportIssue(i, "question", "required", "question required"))
        elif len(str(q)) > 20_000:
            issues.append(ImportIssue(i, "question", "too_long", "question exceeds 20000"))
        if a is None or str(a).strip() == "":
            issues.append(
                ImportIssue(i, "expected_answer", "required", "expected_answer required for seed")
            )
        ext = str(row.get("external_case_id") or f"row-{i}")
        if ext in seen_ext:
            issues.append(ImportIssue(i, "external_case_id", "duplicate", "duplicate in import"))
        seen_ext.add(ext)
    return issues


def import_qa(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    benchmark_id: uuid.UUID,
    content: str,
    fmt: str,
    actor_id: uuid.UUID | None,
    validate_only: bool = False,
    suite: str = "fixed_core",
) -> ImportResult:
    rows = _parse_qa_rows(content, fmt)
    issues = validate_qa_rows(rows)
    if issues:
        return ImportResult(valid=False, issues=issues, case_count=len(rows))
    if validate_only:
        return ImportResult(valid=True, case_count=len(rows), artifact_hash=content_hash(content))

    benchmark = session.get(m.Benchmark, benchmark_id)
    if benchmark is None or benchmark.tenant_id != tenant_id or benchmark.project_id != project_id:
        return ImportResult(
            valid=False,
            issues=[ImportIssue(0, "benchmark_id", "not_found", "benchmark not in scope")],
        )

    existing_versions = list(
        session.execute(
            select(m.BenchmarkVersion).where(m.BenchmarkVersion.benchmark_id == benchmark_id)
        ).scalars()
    )
    next_version = max((v.version for v in existing_versions), default=0) + 1
    artifact = content_hash(content)

    case_ids: list[uuid.UUID] = []
    for i, row in enumerate(rows, start=1):
        ext = str(row.get("external_case_id") or f"row-{i}")
        prev = session.execute(
            select(m.CaseRevision)
            .where(
                m.CaseRevision.benchmark_id == benchmark_id,
                m.CaseRevision.external_case_id == ext,
            )
            .order_by(m.CaseRevision.revision.desc())
        ).scalars().first()
        rev_n = (prev.revision + 1) if prev else 1
        case = m.CaseRevision(
            tenant_id=tenant_id,
            project_id=project_id,
            benchmark_id=benchmark_id,
            external_case_id=ext,
            revision=rev_n,
            question=str(row["question"]),
            expected_answer=str(row.get("expected_answer") or ""),
            multi_turn=json.loads(row["multi_turn"])
            if isinstance(row.get("multi_turn"), str) and row.get("multi_turn")
            else row.get("multi_turn"),
            language=row.get("language"),
            task_category=row.get("task_category"),
            rubric=json.loads(row["rubric"])
            if isinstance(row.get("rubric"), str) and row.get("rubric")
            else row.get("rubric"),
            document_labels=_as_list(row.get("document_labels")),
            permitted_tools=_as_list(row.get("permitted_tools")),
            permitted_skills=_as_list(row.get("permitted_skills")),
            fixtures=row.get("fixtures")
            if isinstance(row.get("fixtures"), dict)
            else (json.loads(row["fixtures"]) if row.get("fixtures") else None),
            is_seed=True,
            sample_weight=Decimal(str(row.get("sample_weight") or 1)),
            provenance={"import_artifact": artifact, "actor": str(actor_id) if actor_id else None},
        )
        session.add(case)
        session.flush()
        case_ids.append(case.id)

    manifest_src = json.dumps(
        [{"case_id": str(c), "suite": suite} for c in case_ids], sort_keys=True
    )
    version = m.BenchmarkVersion(
        tenant_id=tenant_id,
        project_id=project_id,
        benchmark_id=benchmark_id,
        version=next_version,
        manifest_hash=content_hash(manifest_src),
        artifact_hash=artifact,
        published_by=actor_id,
        notes="seed import" if next_version == 1 else "qa import",
    )
    session.add(version)
    session.flush()
    for cid in case_ids:
        session.add(
            m.SuiteMembership(
                benchmark_version_id=version.id,
                case_revision_id=cid,
                suite=suite,
                tenant_id=tenant_id,
                project_id=project_id,
            )
        )
    session.flush()
    return ImportResult(
        valid=True,
        case_count=len(case_ids),
        version_id=version.id,
        version=next_version,
        artifact_hash=artifact,
    )


def _as_list(value: Any) -> list[Any] | None:
    if value is None or value == "":
        return None
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else [value]
        except json.JSONDecodeError:
            return [v.strip() for v in value.split(",") if v.strip()]
    return [value]


def refresh_benchmark(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    benchmark_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    dry_run: bool = False,
    participating_agent_ids: set[uuid.UUID] | None = None,
) -> dict[str, Any]:
    """Sample production traffic into candidates; serialize publication per project/benchmark."""
    now = datetime.now(UTC)
    wm_row = session.execute(
        select(m.RefreshWatermark).where(
            m.RefreshWatermark.tenant_id == tenant_id,
            m.RefreshWatermark.project_id == project_id,
            m.RefreshWatermark.benchmark_id == benchmark_id,
        )
    ).scalar_one_or_none()
    lower = wm_row.watermark if wm_row else datetime(1970, 1, 1, tzinfo=UTC)
    upper = now

    # eligible completed production sessions with outcomes in window
    q = select(m.Session).where(
        m.Session.tenant_id == tenant_id,
        m.Session.project_id == project_id,
        m.Session.traffic_tag == "production",
        m.Session.ended_at.is_not(None),
        m.Session.ended_at > lower,
        m.Session.ended_at <= upper,
    )
    if participating_agent_ids is not None:
        q = q.where(m.Session.agent_id.in_(list(participating_agent_ids)))
    sessions = list(session.execute(q).scalars())

    # exclude system/test already via traffic_tag
    # oversample failures
    failures = [s for s in sessions if s.business_success is False or s.state in ("abandoned", "timed_out")]
    ordinary = [s for s in sessions if s not in failures]
    sample = failures[:50] + ordinary[:50]

    proposed = 0
    needs_review = 0
    preview = []
    for s in sample:
        # low rating => review evidence, not expected answer
        low_rating = False
        feedbacks = list(
            session.execute(
                select(m.FeedbackRevision).where(
                    m.FeedbackRevision.tenant_id == tenant_id,
                    m.FeedbackRevision.project_id == project_id,
                    m.FeedbackRevision.agent_id == s.agent_id,
                    m.FeedbackRevision.subject_type == "session",
                    m.FeedbackRevision.subject_id == s.external_session_id,
                )
            ).scalars()
        )
        for f in feedbacks:
            if f.score is not None and float(f.score) <= 2 and not f.retracted:
                low_rating = True

        question = (s.metadata_ or {}).get("question") or f"session:{s.external_session_id}"
        observed = (s.metadata_ or {}).get("answer")
        status = "needs_review" if (low_rating or not observed) else "proposed"
        expected = None  # production cannot certify its own answer
        if dry_run:
            preview.append(
                {
                    "session_id": str(s.id),
                    "status": status,
                    "question": question,
                    "low_rating": low_rating,
                }
            )
            continue
        cand = m.Candidate(
            tenant_id=tenant_id,
            project_id=project_id,
            benchmark_id=benchmark_id,
            status=status,
            question=str(question),
            observed_outcome=str(observed) if observed else None,
            expected_answer=expected,
            source_agent_id=s.agent_id,
            evidence_refs=[{"session_id": str(s.id), "external": s.external_session_id}],
            review_reason="low_rating" if low_rating else ("missing_answer" if not observed else None),
            split="developer" if hash(str(s.id)) % 5 else "holdout",
        )
        session.add(cand)
        proposed += 1
        if status == "needs_review":
            needs_review += 1

    if dry_run:
        return {
            "dry_run": True,
            "window": {"lower": lower.isoformat(), "upper": upper.isoformat()},
            "sample_size": len(sample),
            "preview": preview[:20],
        }

    session.flush()
    # watermark advanced only when intake durable — here candidates are flushed
    if wm_row is None:
        wm_row = m.RefreshWatermark(
            tenant_id=tenant_id,
            project_id=project_id,
            benchmark_id=benchmark_id,
            watermark=upper,
        )
        session.add(wm_row)
    else:
        wm_row.watermark = upper
    session.flush()

    no_change = proposed == 0
    return {
        "dry_run": False,
        "candidate_count": proposed,
        "needs_review": needs_review,
        "no_change": no_change,
        "watermark": upper.isoformat(),
    }


def review_candidate(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    candidate_id: uuid.UUID,
    decision: str,
    expected_revision: int,
    actor_id: uuid.UUID,
    expected_answer: str | None = None,
    reason: str | None = None,
) -> m.Candidate:
    cand = session.get(m.Candidate, candidate_id)
    if cand is None or cand.tenant_id != tenant_id or cand.project_id != project_id:
        raise LookupError("candidate_not_found")
    if cand.revision != expected_revision:
        raise ConflictError("optimistic_concurrency")
    if decision not in ("approved", "rejected", "validated"):
        raise ValueError("bad_decision")
    cand.status = decision
    cand.revision += 1
    cand.reviewed_at = datetime.now(UTC)
    cand.reviewed_by = actor_id
    if expected_answer is not None:
        cand.expected_answer = expected_answer
    if reason:
        cand.review_reason = reason
    session.flush()
    return cand


class ConflictError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def publish_version_from_candidates(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    benchmark_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    include_seed_suites: bool = True,
) -> m.BenchmarkVersion:
    """Atomically publish approved candidates + prior fixed_core into a new version."""
    approved = list(
        session.execute(
            select(m.Candidate).where(
                m.Candidate.tenant_id == tenant_id,
                m.Candidate.project_id == project_id,
                m.Candidate.benchmark_id == benchmark_id,
                m.Candidate.status == "approved",
            )
        ).scalars()
    )
    existing = list(
        session.execute(
            select(m.BenchmarkVersion)
            .where(m.BenchmarkVersion.benchmark_id == benchmark_id)
            .order_by(m.BenchmarkVersion.version.desc())
        ).scalars()
    )
    next_version = (existing[0].version + 1) if existing else 1
    prev = existing[0] if existing else None

    memberships: list[tuple[uuid.UUID, str]] = []
    if prev and include_seed_suites:
        for sm in session.execute(
            select(m.SuiteMembership).where(m.SuiteMembership.benchmark_version_id == prev.id)
        ).scalars():
            memberships.append((sm.case_revision_id, sm.suite))

    for cand in approved:
        case = m.CaseRevision(
            tenant_id=tenant_id,
            project_id=project_id,
            benchmark_id=benchmark_id,
            external_case_id=f"cand-{cand.id}",
            revision=1,
            question=cand.question,
            expected_answer=cand.expected_answer,
            source_agent_id=cand.source_agent_id,
            evidence_refs=cand.evidence_refs,
            provenance={"candidate_id": str(cand.id)},
            holdout=cand.split == "holdout",
            is_seed=False,
        )
        session.add(case)
        session.flush()
        suite = "rolling_production" if cand.split != "holdout" else "rolling_production"
        if cand.review_reason == "regression":
            suite = "regression"
        memberships.append((case.id, suite))
        cand.status = "published"
        cand.case_revision_id = case.id

    manifest_src = json.dumps(
        [{"case_id": str(c), "suite": s} for c, s in sorted(memberships, key=lambda x: str(x[0]))],
        sort_keys=True,
    )
    version = m.BenchmarkVersion(
        tenant_id=tenant_id,
        project_id=project_id,
        benchmark_id=benchmark_id,
        version=next_version,
        manifest_hash=content_hash(manifest_src),
        published_by=actor_id,
        watermark=datetime.now(UTC),
    )
    session.add(version)
    session.flush()
    for case_id, suite in memberships:
        session.add(
            m.SuiteMembership(
                benchmark_version_id=version.id,
                case_revision_id=case_id,
                suite=suite,
                tenant_id=tenant_id,
                project_id=project_id,
            )
        )
    session.flush()
    return version


def get_manifest(session: Session, version_id: uuid.UUID) -> dict[str, Any]:
    version = session.get(m.BenchmarkVersion, version_id)
    if version is None:
        raise LookupError("version_not_found")
    members = list(
        session.execute(
            select(m.SuiteMembership).where(m.SuiteMembership.benchmark_version_id == version_id)
        ).scalars()
    )
    cases = []
    for sm in members:
        case = session.get(m.CaseRevision, sm.case_revision_id)
        if case is None:
            continue
        cases.append(
            {
                "case_revision_id": str(case.id),
                "external_case_id": case.external_case_id,
                "revision": case.revision,
                "suite": sm.suite,
                "holdout": case.holdout,
                "question_hash": hashlib.sha256(case.question.encode()).hexdigest()[:16],
            }
        )
    cases.sort(key=lambda c: (c["suite"], c["external_case_id"], c["revision"]))
    return {
        "version_id": str(version.id),
        "version": version.version,
        "manifest_hash": version.manifest_hash,
        "cases": cases,
    }
