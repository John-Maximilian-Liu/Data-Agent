# Data Agent — Agent Instructions

## Project

Multi-tenant observability, analytics, benchmarking, and experiment platform for production agents.

- **Backend**: modular monolith (`dataagent`) — FastAPI API + background Worker, PostgreSQL
- **SDK**: independently installable `dataagent-sdk` (client libraries only)
- **LLM provider**: Azure OpenAI for every production LLM call made by this system
- **Isolation**: tenant → project → agent from first migration, ingestion route, and job

## Layout

```
dataagent/           # server package (API, ingestion, analytics, benchmarks, experiments, auth, jobs, providers, storage)
packages/dataagent-sdk/  # installable SDK
alembic/             # migrations
tests/               # pytest (unit + PostgreSQL integration)
fixtures/            # synthetic events, OTLP, QA, auth matrix
deploy/              # AKS + Alibaba Sandbox overlays
docs/                # operator runbooks and definitions
examples/            # API/SDK examples
scripts/             # local demo and enqueue helpers
```

## Conventions

- Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2, psycopg3, Alembic, httpx, official OpenAI client, azure-identity, pytest
- Persist UTC instants; project timezone is IANA; rates/currency are Decimal
- Scope every identity constraint, FK, and query by tenant/project (and agent when agent-owned)
- No external agent/eval/observability framework SDKs in the backend — HTTP/JSON, OpenAPI, OIDC, OTLP/HTTP only
- Secrets never in logs or API responses
- Offline mode: fake agent + fake Azure transport when credentials unavailable
- Production writes / live deploys only for explicitly authorized targets

## Commands

```bash
# Install (editable) + migrate + test
pip install -e ".[dev]" -e "./packages/dataagent-sdk"
export DATABASE_URL=postgresql+psycopg://dataagent:dataagent@127.0.0.1:5432/dataagent
alembic upgrade head
pytest -q

# Local demo (synthetic, offline)
python -m dataagent.cli demo

# Entry points
dataagent api | worker | enqueue-daily | migrate | smoke
```

## Auth modes

- Production: OIDC (signature, issuer, audience, expiration) + tenant membership
- Offline/dev: `DATAAGENT_AUTH_MODE=offline` only; production startup rejects offline mode

## Modules

Project settings toggle: sessions, feedback, cost, activity, kb, tools, skills, benchmark, experiments, reports.
Module availability is evaluated after scope authorization.
