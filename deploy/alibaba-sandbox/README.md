# Phase 2 — Alibaba Cloud Agent Sandbox Worker

Preserve the **same image digest** as AKS. PostgreSQL, secrets, stable API address, and the scheduler remain outside Sandbox lifetime.

## Prerequisites (live checks — pending until authorized)

Official documentation must be verified for the selected edition/region before production cutover:

| Check | Command / action | Prerequisite |
|-------|------------------|--------------|
| Image format & ports | Consult Alibaba Cloud Agent Sandbox docs for container image requirements | Sandbox edition + region chosen |
| Pull image | `docker pull $IMAGE_REF` then push to the registry the Sandbox uses | Registry credentials |
| Network egress to PostgreSQL | From Sandbox: `psql "$DATAAGENT_DATABASE_URL" -c 'select 1'` | Private link / allowlist |
| Network egress to Azure OpenAI | `curl -I "$DATAAGENT_AZURE_OPENAI_ENDPOINT"` | Egress policy |
| Azure federation | Validate Entra workload federation for Sandbox identity; fallback: inject `DATAAGENT_AZURE_OPENAI_API_KEY` at provider level | Security review |
| Startup / idle limits | Confirm active and idle timeouts; set lease/heartbeat below idle limit | Edition docs |

> A development simulation cannot establish provider availability. Record live results in the operator log when credentials are authorized.

## Configurable deploy path

```bash
# 1. Resolve the digest already running in AKS (do not rebuild)
DIGEST=$(kubectl -n dataagent get deploy dataagent-worker -o jsonpath='{.spec.template.spec.containers[0].image}')
echo "Using $DIGEST"

# 2. Render sandbox overlay (edition-specific values)
export IMAGE_REF="$DIGEST"
export DATABASE_URL_SECRET=...          # outside Sandbox
export API_BASE_URL=https://dataagent.example.com
export WORKER_ID=sandbox-worker-1
envsubst < deploy/alibaba-sandbox/worker.env.template > /tmp/sandbox.env

# 3. Launch replacement Worker (operator command — replace with edition CLI)
# PENDING LIVE: <sandbox-cli> run --image "$IMAGE_REF" --env-file /tmp/sandbox.env --cmd worker
echo "PENDING LIVE: sandbox run --image $IMAGE_REF --cmd worker"

# 4. Migration drill (shared PostgreSQL)
# - Preflight: image pull, DB, DNS, Azure auth
# - Start Sandbox Worker; verify / smoke claim against same DB
# - Stop AKS Worker from claiming (scale to 0); drain leases
# - Verify freshness, backlog, no duplicate logical keys
# - Rollback: pause Sandbox Worker; scale AKS Worker back up
```

## Environment matrix

| Concern | AKS | Alibaba Sandbox |
|---------|-----|-----------------|
| Image | `IMAGE_REF` digest | **same digest** |
| Entry point | `dataagent worker` | `dataagent worker` |
| DB | Azure PG / flexible server | Same DSN (external) |
| Scheduler | CronJob `enqueue-daily` | Same CronJob on AKS (or external) |
| API | AKS Service | Remains on AKS (optional later move) |
| Azure auth | Workload ID | Federation if available; else key injection |
| Lifecycle | Deployment + preStop | Edition idle/active limits (configurable) |

## Citations

Operator must attach official doc URLs for the chosen edition/region when completing live checks. Until then, treat Sandbox availability as **unverified**.
