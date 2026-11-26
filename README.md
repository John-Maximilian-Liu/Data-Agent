# Data Agent

**Languages:** **English** · [中文](README.zh.md) · [日本語](README.ja.md) · [Deutsch](README.de.md)

**From live agent usage to auditable release decisions.**

A multi-tenant platform that observes production agents, produces daily metrics and charts, maintains versioned benchmarks from seed Q&A and governed traffic, and compares candidate releases against current production.

<p align="center">
  <img src="docs/assets/architecture.png" alt="Data Agent architecture — From live agent usage to release decisions" width="100%" />
</p>

<p align="center"><em>From live agent usage to release decisions — multi-tenant analytics, evolving benchmarks, and pre-release evaluation</em></p>

---

## Table of contents

1. [What it solves](#1-what-it-solves)
2. [How the system works](#2-how-the-system-works)
3. [Get running in 5 minutes](#3-get-running-in-5-minutes)
4. [Core concepts](#4-core-concepts)
5. [Get started by role](#5-get-started-by-role)
6. [Ingest production traffic with the SDK](#6-ingest-production-traffic-with-the-sdk)
7. [Daily analytics and charts](#7-daily-analytics-and-charts)
8. [Benchmarks: import, refresh, publish](#8-benchmarks-import-refresh-publish)
9. [Pre-release experiments](#9-pre-release-experiments)
10. [Permissions and module flags](#10-permissions-and-module-flags)
11. [HTTP API cheat sheet](#11-http-api-cheat-sheet)
12. [Deploy and migrate](#12-deploy-and-migrate)
13. [Configuration and troubleshooting](#13-configuration-and-troubleshooting)
14. [Further reading](#14-further-reading)

---

## 1. What it solves

| Pain | What Data Agent does |
|------|----------------------|
| Shared observability across agents/teams leaks access | Hard boundary **Tenant → Project → Agent → Release**; roles bind to agent, group, or whole project |
| Heterogeneous telemetry (SDK, OTLP, legacy JSON) | Normalize to canonical events; OTLP and versioned declarative JSON mappings with dry-run |
| Drifting metric definitions / cross-agent double counts | Metric registry + project timezone day cuts; **filter authorized agents before aggregation** |
| Hand-built benchmarks, not comparable across releases | Seed QA → daily candidates → review → publish; historical manifests stay runnable |
| “Feels better” release decisions | Same benchmark version: baseline vs candidate; rules + Azure OpenAI judging; gates independent of narrative |

**Principle:** Access rules apply from intake through reporting. Default deny. Sensitive fields (cost, raw text, holdout, judge detail) need explicit grants.

---

## 2. How the system works

Left to right in the diagram:

```
Production agents / feedback / legacy systems / seed QA
        │
        ▼
   Intake & validation (auth · normalize · dedupe · redact)
        │
        ├─► Daily analytics (DAU/MAU, ratings, cost, tool/skill ranking)
        ├─► Benchmark updates (candidates → review → publish core / rolling / regression)
        └─► Pre-release evaluation (baseline vs candidate · scoring · regression gates)
                │
                ▼
     Business dashboards · Admins/reviewers · Developers · SDK / HTTP API
```

- **Storage & jobs:** PostgreSQL holds events, benchmarks, permissions, and jobs. Workers claim work with leases and fencing tokens; unfinished work can resume.
- **LLM calls:** Judge and report narrative in this system always use **Azure OpenAI** (local default: `fake` transport, no real credentials required).
- **Deploy:** Phase 1 runs API + Worker on AKS. Phase 2 can move the Worker to Alibaba Cloud Agent Sandbox with the **same image digest**; DB, secrets, stable API, and scheduler stay outside the sandbox.

---

## 3. Get running in 5 minutes

### Prerequisites

- Python **3.12+**
- PostgreSQL **16+** (local or Docker)

### Docker Compose (recommended)

```bash
git clone https://github.com/John-Maximilian-Liu/Data-Agent.git
cd Data-Agent
docker compose up --build
# API: http://127.0.0.1:8000
# Health: curl http://127.0.0.1:8000/readyz
```

### Local development

```bash
# 1) Database
export DATAAGENT_DATABASE_URL=postgresql+psycopg://dataagent:dataagent@127.0.0.1:5432/dataagent
export DATAAGENT_AUTH_MODE=offline          # development only; rejected in production
export DATAAGENT_AZURE_TRANSPORT=fake       # offline fake Azure — no real key needed
export DATAAGENT_ENVIRONMENT=development

# 2) Install
pip install -e ".[dev]" -e "./packages/dataagent-sdk"

# 3) Migrate + smoke
dataagent migrate    # or: alembic upgrade head
dataagent smoke      # expect: schema_ok

# 4) Start (two terminals)
dataagent api        # :8000
dataagent worker

# 5) Offline demo: seed tenants/agents, ingest, benchmark v1, run experiment
dataagent demo
```

`demo` prints JSON — keep `project_id`, `agents`, and `experiment_id` for later examples.

### Health and docs

| URL | Purpose |
|-----|---------|
| `GET /healthz` | Liveness |
| `GET /readyz` | Readiness (includes schema check) |
| `GET /docs` | Swagger UI (OpenAPI) |
| `GET /v1/meta` | System roles and default modules |

Offline auth uses a request header:

```http
X-Dataagent-Principal: demo-admin
```

---

## 4. Core concepts

### Hierarchy

| Level | Meaning |
|-------|---------|
| **Tenant** | Hard org boundary — no cross-tenant data sharing |
| **Project** | Workspace with timezone, module flags, sources, and benchmarks |
| **Agent** | Observed / evaluated application; status: `active` / `paused` / `archived` |
| **Release** | Stable agent version; register test endpoint or CI target |
| **Agent Group** | In-project collection used **only as a scope selector** — grants no permissions by itself |

### Three workflows

1. **Daily analytics** — session volume, completion, business success, ratings, DAU/MAU, tokens/cost, top KB/tools/skills, latency percentiles  
2. **Benchmark updates** — seed QA import → sample production traffic → review → publish (`fixed_core` / `rolling_production` / `regression`)  
3. **Pre-release evaluation** — pick `benchmark_version` + baseline/candidate releases → execute → score → gate (`pass` / `fail` / `inconclusive`)

### Intake paths

| Path | Use when |
|------|----------|
| Canonical `POST .../events:batch` | Python SDK / custom collectors |
| `POST .../otlp/v1/traces` | OpenTelemetry OTLP/HTTP |
| `POST .../sources/{id}/webhook` | Legacy JSON + versioned mapping |
| `POST .../feedback` | Rating / outcome revisions |

Every event needs: `event_id`, `event_type`, `schema_version`, `occurred_at`, `payload`. The server stamps `received_at` and derives tenant / project / source from authentication.

---

## 5. Get started by role

| You are | First steps |
|---------|-------------|
| **Platform / Project Admin** | Register agents & sources → issue credentials → set modules & role bindings → trigger benchmark refresh |
| **Benchmark Reviewer** | List candidates → approve/reject (bound to a revision) → never leak holdout |
| **Agent Developer** | Register releases → start experiments → read results/reports for authorized agents only |
| **Analyst / Viewer** | Query `/analytics/metrics` and charts; cost and similar fields need extra grants |
| **Service Account** | Use source / CI credentials only to write events or submit private CI results |

**Example: Admin registers an agent (offline)**

```bash
export BASE=http://127.0.0.1:8000
export PROJECT_ID=<project_id from demo>
export H='-H X-Dataagent-Principal:demo-admin -H Content-Type:application/json'

curl -s $H -X POST "$BASE/v1/projects/$PROJECT_ID/agents" \
  -d '{"slug":"support-bot","name":"Support Bot","owner_team":"cx"}'
```

**Example: Bind a viewer role to Agent Alpha only**

```bash
curl -s $H -X POST "$BASE/v1/projects/$PROJECT_ID/role-bindings" \
  -d '{
    "principal_id":"<analyst-principal-uuid>",
    "role_name":"viewer",
    "agent_id":"<alpha-agent-uuid>",
    "reason":"read aggregates for alpha only"
  }'
```

Inspect your effective access:

```bash
curl -s $H "$BASE/v1/projects/$PROJECT_ID/access/effective"
```

---

## 6. Ingest production traffic with the SDK

The SDK depends only on `httpx` and `pydantic` — installable without the server package.

```bash
pip install -e ./packages/dataagent-sdk
# after publish: pip install dataagent-sdk
```

### Minimal ingest example

```python
from dataagent_sdk import Client

client = Client(
    base_url="http://127.0.0.1:8000",
    principal="demo-admin",          # offline development
    # credential="da_xxx...",        # production: source / agent credential
)

project_id = "..."
source_id = "..."

events = [
    {
        "event_id": "evt-1001",
        "event_type": "session.start",
        "schema_version": "1.0",
        "occurred_at": "2024-06-01T03:30:00Z",
        "agent_id": "<agent-uuid>",
        "payload": {"session_id": "s-1", "user_id": "u-42"},
    },
    {
        "event_id": "evt-1002",
        "event_type": "session.end",
        "schema_version": "1.0",
        "occurred_at": "2024-06-01T03:45:00Z",
        "agent_id": "<agent-uuid>",
        "payload": {
            "session_id": "s-1",
            "state": "completed",
            "technical_completed": True,
            "business_success": True,
        },
    },
    {
        "event_id": "evt-1003",
        "event_type": "model.call",
        "schema_version": "1.0",
        "occurred_at": "2024-06-01T03:40:00Z",
        "agent_id": "<agent-uuid>",
        "payload": {
            "provider_call_id": "call-abc",
            "model": "gpt-4o",
            "input_tokens": 120,
            "output_tokens": 40,
            "usage_class": "production",
        },
    },
]

result = client.events.batch(
    project_id,
    source_id=source_id,
    events=events,
    idempotency_key="batch-2024-06-01-1",  # safe to retry
)
print(result)
# {"accepted": 3, "duplicate": 0, "rejected": 0, "content_conflict": 0, ...}
```

### Supported `event_type` values

`session.start` · `session.end` · `run.completed` · `feedback.revision` · `model.call` · `retrieval` · `tool.call` · `skill.execution` · `span`

### Batch semantics (important)

| Status | Meaning |
|--------|---------|
| `accepted` | Committed |
| `duplicate` | Same scope/source/`event_id` and content hash (replay-safe) |
| `content_conflict` | Same `event_id`, different content |
| `rejected` | Not written (e.g. agent not on source allowlist); **does not leak** whether the agent exists outside the grant |

### Expose the SDK as tools

```python
tools = client.as_tools(project_id)
# Authorized reads by default; mutations need explicit scopes
for t in tools:
    print(t["name"], "mutation=" + str(t.get("mutation")))
```

More: [`examples/sdk_quickstart.py`](examples/sdk_quickstart.py), [`packages/dataagent-sdk/README.md`](packages/dataagent-sdk/README.md).

---

## 7. Daily analytics and charts

### Schedule daily metrics

```bash
dataagent enqueue-daily --date 2024-06-01
# Worker claims jobs with kind=analytics.daily
```

### Query metrics / charts

```bash
curl -s $H "$BASE/v1/projects/$PROJECT_ID/analytics/metrics\
?metric=session_volume&start=2024-06-01&end=2024-06-07"

curl -s $H "$BASE/v1/projects/$PROJECT_ID/analytics/charts/session_volume\
?start=2024-06-01&end=2024-06-07"
```

### Metric catalog

| metric | Description |
|--------|-------------|
| `session_volume` | Production sessions by local day (incl. state dimension) |
| `completion_rate` / `success_rate` | Technical completion vs business success (known outcomes only in denominator) |
| `ratings` | Scales kept separate; latest effective revision wins per rater/subject |
| `dau` / `mau` | Pseudonymous user dedupe; cross-agent queries dedupe again |
| `tokens` / `cost` | Input/output/cache; cost requires the `cost` field grant |
| `top_kb` / `top_tools` / `top_skills` | Stable ID ranking with deterministic ties |
| `latency` | End-to-end run wall-time P50/P95 |

Definitions: [`docs/ANALYTICS.md`](docs/ANALYTICS.md). Chart sample: [`fixtures/charts/session_volume.example.json`](fixtures/charts/session_volume.example.json).

---

## 8. Benchmarks: import, refresh, publish

### ① Create a benchmark and import seed QA (→ v1)

Seed file: CSV or JSONL with required `question` and `expected_answer`; optional `external_case_id`, `language`, `task_category`, `rubric`, `document_labels`, `permitted_tools`, etc.

```bash
curl -s $H -X POST "$BASE/v1/projects/$PROJECT_ID/benchmarks" \
  -d '{"slug":"core","name":"Core QA","ownership":"agent_scoped","owner_agent_id":"<agent>"}'

curl -s $H -X POST "$BASE/v1/projects/$PROJECT_ID/benchmarks/$BENCH_ID/qa-imports" \
  -d "$(jq -n --rawfile c fixtures/qa/seed.jsonl \
    '{content:$c, format:"jsonl", validate_only:false, suite:"fixed_core"}')"
```

Validation failures return **row + field + error code**; commit is atomic only when every row is valid. First published seed import creates **v1**.

### ② Refresh candidates from production (Admin)

```bash
curl -s $H -X POST "$BASE/v1/projects/$PROJECT_ID/benchmarks/$BENCH_ID/refreshes" \
  -d '{"dry_run":true}'

curl -s $H -X POST "$BASE/v1/projects/$PROJECT_ID/benchmarks/$BENCH_ID/refreshes" \
  -d '{"dry_run":false}'
# → 202 + job_id
```

Pipeline: sample from watermark → stratify / oversample failures → redact → propose candidates (`proposed` / `needs_review`…). **Production traffic cannot certify its own expected answer**; low ratings become review evidence only.

### ③ Review and publish a new version

```bash
curl -s $H "$BASE/v1/projects/$PROJECT_ID/benchmarks/$BENCH_ID/candidates"

curl -s $H -X POST \
  "$BASE/v1/projects/$PROJECT_ID/benchmarks/$BENCH_ID/candidates/$CAND_ID/review" \
  -d '{"decision":"approved","expected_revision":1,"expected_answer":"Paris"}'

curl -s $H -X POST "$BASE/v1/projects/$PROJECT_ID/benchmarks/$BENCH_ID/versions"
# returns version + manifest_hash; older versions remain runnable
```

Suites: `fixed_core` · `rolling_production` · `regression`.

---

## 9. Pre-release experiments

### Register releases

```bash
curl -s $H -X POST "$BASE/v1/projects/$PROJECT_ID/releases" \
  -d '{"agent_id":"<agent>","version":"1.0.0","is_production":true}'

curl -s $H -X POST "$BASE/v1/projects/$PROJECT_ID/releases" \
  -d '{"agent_id":"<agent>","version":"1.1.0-rc1","endpoint_url":"https://test.example/v1/chat"}'
```

### Start an experiment (202 + job)

```bash
curl -s $H -X POST "$BASE/v1/projects/$PROJECT_ID/experiments" \
  -d '{
    "agent_id":"<agent>",
    "benchmark_version_id":"<version-uuid>",
    "baseline_release_id":"<baseline-release>",
    "candidate_release_id":"<candidate-release>",
    "suites":["fixed_core","regression"],
    "evaluators":["task_success","rag","tools","skills","observable_path","prompt_behavior","runtime_cost"]
  }'
```

After the worker finishes:

```bash
curl -s $H "$BASE/v1/projects/$PROJECT_ID/experiments/$EXP_ID"
curl -s $H "$BASE/v1/projects/$PROJECT_ID/experiments/$EXP_ID/results"
curl -s $H "$BASE/v1/projects/$PROJECT_ID/experiments/$EXP_ID/report"
```

### How to read gates

- Quality / reliability / latency / cost are judged **independently**  
- Overall: `pass` | `fail` | `inconclusive`  
- Missing critical evidence or too few pairs → `inconclusive` (unless a hard failure already decided `fail`)  
- **Missing traces ≠ zero scores** (recorded as missing)  
- Azure narrative text **does not** change gate decisions  

Private CI can submit results to `/experiments/{id}/ci-results` with a `ci.submit` credential and a deduplicating `submission_key`.

Report sample: [`fixtures/reports/experiment.example.json`](fixtures/reports/experiment.example.json).

---

## 10. Permissions and module flags

### System roles (starting point)

| Role | Summary |
|------|---------|
| Tenant Admin | Create projects, delegate project admin; **raw text / cost still need separate data grants** |
| Project Admin | Agents/sources/modules/groups/delegation/trigger refresh |
| Agent Owner | Config and test runs for allocated agents — no automatic project/tenant admin |
| Agent Developer | Metrics/traces; run/review experiments |
| Benchmark Reviewer | Review granted datasets or agent-scoped candidates |
| Analyst / Viewer | Read authorized aggregates; cost/drill-down/raw/export need field grants |
| Service Account | Bound `source.write` / `ci.submit` only |

Project binding selector (**exactly one**): single agent · agent group · `whole_project=true`.  
**Never** combine “cost on Agent A” with “traces on Agent B” to reveal B’s costs.

### Module flags

Projects may disable: `sessions` `feedback` `cost` `activity` `kb` `tools` `skills` `benchmark` `experiments` `reports`.  
Modules are checked **after** scope authorization. QA-only evaluation is allowed when production text capture is off.

```bash
curl -s $H -X PATCH "$BASE/v1/projects/$PROJECT_ID/settings" \
  -d '{"modules":{"cost":false,"reports":true}}'
```

---

## 11. HTTP API cheat sheet

Business routes live under `/v1/projects/{project_id}`.

| Family | Paths | Purpose |
|--------|-------|---------|
| Catalog | `/agents` `/agent-groups` | Register, pause, archive, group membership |
| Access | `/roles` `/memberships` `/role-bindings` `/access/effective` | Grants and self-inspection |
| Sources | `/sources` `.../mapping-revisions` `.../credentials` | Mapping dry-run/publish, credential rotation |
| Intake | `/events:batch` `/feedback` `/otlp/v1/traces` `.../webhook` | Three intake paths |
| Analytics | `/analytics/metrics` `/analytics/charts/{metric}` | Tables and Plotly JSON |
| Benchmarks | `/benchmarks` `.../qa-imports` `.../refreshes` `.../candidates` `.../versions` | Lifecycle |
| Experiments | `/experiments` `.../results` `.../report` `.../cancel` `.../ci-results` | Release comparison |
| Jobs | `/jobs/{id}` | Poll async 202 work |
| Settings | `/settings` | Modules and project config |

Conventions:

- Async creates → **HTTP 202** + `job_id` + `status_url`  
- Retryable writes accept `Idempotency-Key` (scoped by project + operation)  
- Stable error codes + `X-Request-Id`  
- OpenAPI at `/docs` while the API is running  

Shell sample: [`examples/api_quickstart.sh`](examples/api_quickstart.sh).

---

## 12. Deploy and migrate

| Phase | Content |
|-------|---------|
| **Phase 1** | AKS: `deploy/aks/` — API + Worker Deployments, Service, daily CronJob, migrate Job; Azure OpenAI via Entra Workload ID |
| **Phase 2** | Move Worker to Alibaba Cloud Agent Sandbox: `deploy/alibaba-sandbox/` — **same image digest**; DB / secrets / stable API / scheduler stay outside |

```bash
docker build -t dataagent:local .
# entrypoints: api | worker | enqueue-daily | migrate | smoke
docker run --rm dataagent:local smoke
```

Interruption / replacement drill: [`docs/MIGRATION_DRILL.md`](docs/MIGRATION_DRILL.md).

---

## 13. Configuration and troubleshooting

### Common environment variables

| Variable | Meaning |
|----------|---------|
| `DATAAGENT_DATABASE_URL` | `postgresql+psycopg://...` (Postgres required) |
| `DATAAGENT_AUTH_MODE` | `offline` (dev) or `oidc` (prod) |
| `DATAAGENT_ENVIRONMENT` | `development` / `test` / `production` (production rejects offline) |
| `DATAAGENT_AZURE_TRANSPORT` | `fake` \| `live` |
| `DATAAGENT_AZURE_OPENAI_ENDPOINT` / `_DEPLOYMENT` | Required for live |
| `DATAAGENT_AZURE_USE_ENTRA` | Default true; API key injection is the fallback |
| `DATAAGENT_HMAC_PEPPER` | User pseudonym HMAC (never commit) |
| `DATAAGENT_WORKER_ID` | Worker instance name (lease owner) |

Ops: [`docs/OPERATOR.md`](docs/OPERATOR.md). Dependency rationale: [`docs/DEPENDENCIES.md`](docs/DEPENDENCIES.md).

### FAQ

| Symptom | Fix |
|---------|-----|
| `readyz` → `schema_missing` | Run `dataagent migrate` first |
| Ingest `rejected` / `agent_not_permitted` | Agent missing from source `allowed_agent_ids`, or agent is `paused`/`archived` |
| `403 not_a_member` | Principal lacks project (or tenant-wide) membership |
| `idempotency_conflict` | Same key, different body — change key or keep body identical |
| Experiment stuck `queued` | Ensure a worker is running: `dataagent worker` |
| No Azure key locally | Keep `DATAAGENT_AZURE_TRANSPORT=fake` |

### Tests

```bash
export DATAAGENT_DATABASE_URL=postgresql+psycopg://dataagent:dataagent@127.0.0.1:5432/dataagent_test
pytest -q -m "not live"
# 1000-agent load baseline: pytest -q -m slow
```

---

## 14. Further reading

| Doc | Content |
|-----|---------|
| [`AGENTS.md`](AGENTS.md) | Conventions for coding agents |
| [`docs/OPERATOR.md`](docs/OPERATOR.md) | Startup, collection, mapping, replay, migration |
| [`docs/ANALYTICS.md`](docs/ANALYTICS.md) | Metric definitions |
| [`docs/MIGRATION_DRILL.md`](docs/MIGRATION_DRILL.md) | Worker replacement drill and live-check list |
| [`docs/CLOSURE_SUMMARY.md`](docs/CLOSURE_SUMMARY.md) | Milestone delivery map |
| [`fixtures/`](fixtures/) | Event, OTLP, QA, chart, and report samples |
| [`deploy/`](deploy/) | AKS and Sandbox manifests |

---

## Packages

| Package | Role |
|---------|------|
| `dataagent` | FastAPI API, Worker, intake, analytics, benchmarks, experiments, auth, jobs |
| `dataagent-sdk` | Independently installable client: `events` / `analytics` / `benchmarks` / `experiments` / `as_tools()` |

**CLI entry points:** `dataagent api` · `worker` · `enqueue-daily` · `migrate` · `smoke` · `demo`

---

## License

Apache-2.0
