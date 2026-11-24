# Dependency rationale

| Package | Why |
|---------|-----|
| FastAPI / Uvicorn | HTTP API, OpenAPI, async-friendly ASGI |
| Pydantic / pydantic-settings | Validated config and request schemas |
| SQLAlchemy 2 + psycopg3 | PostgreSQL ORM with SKIP LOCKED job claims |
| Alembic | Versioned migrations (separate invoke) |
| httpx | SDK + outbound HTTP |
| openai + azure-identity | Azure OpenAI judge/narrative (Entra or key) |
| python-jose | OIDC JWT inspection |
| plotly | Chart JSON (specs generated in Python) |
| pandas | Optional tabular helpers for analytics tests |
| tzdata / zoneinfo | IANA timezone + DST for local dates |
| pytest | Unit + PostgreSQL integration tests |
| protobuf (optional extra `otlp`) | Only if binary OTLP protobuf decoding is enabled |

External agent/eval/observability SDKs are intentionally **not** backend dependencies. Integration is HTTP/JSON, OpenAPI, OIDC, OTLP/HTTP.
