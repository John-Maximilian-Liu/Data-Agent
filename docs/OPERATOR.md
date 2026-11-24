# Operator runbook

## Startup

```bash
export DATAAGENT_DATABASE_URL=postgresql+psycopg://dataagent:dataagent@127.0.0.1:5432/dataagent
export DATAAGENT_AUTH_MODE=offline          # rejected when DATAAGENT_ENVIRONMENT=production
export DATAAGENT_AZURE_TRANSPORT=fake       # use live + endpoint for real Azure

pip install -e ".[dev]" -e "./packages/dataagent-sdk"
dataagent migrate
dataagent smoke
dataagent api &
dataagent worker &
dataagent demo
```

## Data collection settings

Project `modules` flags: sessions, feedback, cost, activity, kb, tools, skills, benchmark, experiments, reports.

Sources declare capabilities (identity, ratings, token detail, retrieval refs, component spans). User IDs are HMAC-pseudonymized with project-scoped pepper (`DATAAGENT_HMAC_PEPPER`).

## Schema mapping

Publish mapping revisions under `/sources/{id}/mapping-revisions`. Dry-run with samples first. Transforms are data-driven (paths, enums, timestamp units, array expand) — no user Python.

## Analytics definitions

See metric registry in `dataagent/analytics/metrics.py`. Local dates use the project IANA timezone. Multi-agent queries re-deduplicate DAU/MAU over the authorized agent set.

## Experiment replay

Experiments snapshot case revisions and release artifact versions. Re-running a Worker after interruption resumes via execution uniqueness `(experiment, case, variant, replicate)` and fencing tokens on jobs.

## Migration (AKS → Sandbox)

See `deploy/alibaba-sandbox/README.md`. Shared PostgreSQL; same image digest; drain leases; rollback by resuming AKS Worker.

## Live checks (pending)

| Item | Command | Prerequisite |
|------|---------|--------------|
| Azure OpenAI live judge | `DATAAGENT_AZURE_TRANSPORT=live DATAAGENT_AZURE_OPENAI_ENDPOINT=... pytest -m live` | Entra or API key authorized |
| AKS apply | `kubectl apply -f deploy/aks/` with `IMAGE_REF` | Cluster + Workload ID |
| Sandbox worker | Edition CLI per `deploy/alibaba-sandbox/README.md` | Edition/region availability |
