# Closure summary — Data Agent milestones

Environment: PostgreSQL 16 local, `DATAAGENT_AUTH_MODE=offline`, `DATAAGENT_AZURE_TRANSPORT=fake`.  
Test command: `pytest -q -m "not live"` → **31 passed** (2026-11-24, Asia/Shanghai).  
Demo: `dataagent demo` → ingested 11 events, benchmark v1, experiment `gate_decision=fail` on planted regression.

## Milestone → paths → proof

| Milestone | Changed paths | Proof | Assumptions / pending |
|-----------|---------------|-------|------------------------|
| 1 Skeleton | `dataagent/storage/*`, `alembic/`, `dataagent/authorization/*`, `dataagent/api/*`, `AGENTS.md` | `alembic upgrade head`; `dataagent smoke` → `schema_ok`; `tests/test_m1_catalog_auth.py` | Empty PG required |
| 2 Intake + analytics | `dataagent/ingestion/*`, `dataagent/analytics/*`, `fixtures/events/*`, `fixtures/otlp/*` | `tests/test_m2_ingest_analytics.py` — native/OTLP/mapping, DST, scoped charts | Fake mapping fixtures labeled illustrative |
| 3 Benchmark | `dataagent/benchmarks/*`, `fixtures/qa/*` | `tests/test_m3_benchmarks.py` — v1 import, refresh→v2, v1 manifest retained | Offline traffic fixtures |
| 4 Experiments | `dataagent/experiments/*`, `dataagent/providers/azure.py` | `tests/test_m4_experiments.py` — tool/task regression, missing traces ≠ 0, CI replay | Azure live judge **pending** |
| 5 SDK + policy | `packages/dataagent-sdk/*`, `tests/test_m5_auth_sdk_load.py`, `dataagent/demo.py` | Auth matrix API tests; SDK `pip show`; load baseline 1000 agents (`@pytest.mark.slow`) | Measured capacity is local baseline only |
| 6 Deployment | `Dockerfile`, `deploy/aks/*`, `deploy/alibaba-sandbox/*`, `docs/MIGRATION_DRILL.md` | Manifests rendered; fencing test; digest-parity procedure documented | Live AKS/Sandbox/Azure smoke **pending** — see table below |

## Pending live checks (exact commands)

| Item | Command | Prerequisite |
|------|---------|--------------|
| Azure OpenAI | `DATAAGENT_AZURE_TRANSPORT=live DATAAGENT_AZURE_OPENAI_ENDPOINT=... DATAAGENT_AZURE_USE_ENTRA=true pytest -m live` | Authorized Entra or key |
| AKS | `IMAGE_REF=... envsubst < deploy/aks/*.yaml \| kubectl apply -f -` | Cluster + Workload ID |
| Image digest parity | `crane digest $AKS_IMAGE; crane digest $SANDBOX_IMAGE` | Registry auth |
| Sandbox Worker | Edition CLI per `deploy/alibaba-sandbox/README.md` | Authorized edition/region; official docs citation at cutover |
| Compose stack | `docker compose up --build` | Docker available |

## Notes

- Offline fake agent + `FakeAzureTransport` used throughout CI-equivalent runs.
- Production writes / cloud deploys were **not** executed (no authorized targets).
- Bright Data / Tavily lookups for Alibaba Sandbox edition docs were unavailable in this environment; Sandbox path is configurable with explicit live-check placeholders.
