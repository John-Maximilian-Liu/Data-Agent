# Worker interruption / replacement drill

Shared PostgreSQL; same image digest for AKS and Sandbox Workers.

## Steps

1. **Preflight (read-only)** — image pull, DB connectivity, DNS, Azure auth.
2. **Launch replacement Worker** against the same DSN with a distinct `DATAAGENT_WORKER_ID`.
3. **Stop old Worker** from claiming (scale to 0 / SIGTERM). Existing leases expire; fencing tokens reject stale checkpoints.
4. **Verify** — job backlog drains, no duplicate `logical_key` completions, experiment executions unique per `(experiment, case, variant, replicate)`, cost uncertainty visible when settle actual is unknown.
5. **Rollback** — pause Sandbox Worker; scale AKS Worker back up.

## Local simulation (offline)

```bash
# Terminal A
DATAAGENT_WORKER_ID=worker-a dataagent worker

# Start an experiment (via API or demo), then kill worker-a (SIGKILL).

# Terminal B — replacement
DATAAGENT_WORKER_ID=worker-b dataagent worker
# Completed executions survive; in-flight cases resume via unique keys + checkpoints.
```

Automated coverage: `tests/test_m5_auth_sdk_load.py::test_job_fencing` and experiment resume uniqueness in `tests/test_m4_experiments.py`.

## Live cloud smoke — PENDING

| Check | Exact command | Prerequisite |
|-------|---------------|--------------|
| AKS apply | `kubectl apply -f deploy/aks/` after substituting `IMAGE_REF` | AKS cluster + Workload ID |
| Image digest parity | `crane digest $AKS_IMAGE` == `crane digest $SANDBOX_IMAGE` | Registry access |
| Sandbox worker | Edition-specific CLI (see `deploy/alibaba-sandbox/README.md`) | Authorized Sandbox edition/region |
| Azure live judge | `DATAAGENT_AZURE_TRANSPORT=live pytest -m live` | Entra or API key |
