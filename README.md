# Data Agent

Multi-tenant platform for observing production agents, computing daily metrics and charts, maintaining versioned benchmarks, and comparing candidate releases.

## Quick start

```bash
# PostgreSQL
export DATAAGENT_DATABASE_URL=postgresql+psycopg://dataagent:dataagent@127.0.0.1:5432/dataagent
export DATAAGENT_AUTH_MODE=offline
export DATAAGENT_AZURE_TRANSPORT=fake

pip install -e ".[dev]" -e "./packages/dataagent-sdk"
alembic upgrade head
pytest -q

# API + worker
dataagent api
dataagent worker
dataagent demo   # synthetic offline demo
```

## Packages

| Package | Role |
|---------|------|
| `dataagent` | FastAPI API, Worker, analytics, benchmarks, experiments |
| `dataagent-sdk` | Independently installable HTTP client + `as_tools()` |

## Architecture

Modular monolith: API and Worker processes, one OCI image, PostgreSQL for durable data and jobs. Azure OpenAI for every production LLM call. See `AGENTS.md` and `docs/`.

## Entry points

`api` · `worker` · `enqueue-daily` · `migrate` · `smoke`

## License

Apache-2.0
