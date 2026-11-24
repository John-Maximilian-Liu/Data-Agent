#!/usr/bin/env bash
set -euo pipefail
BASE=${BASE:-http://127.0.0.1:8000}
H=(-H "X-Dataagent-Principal: demo-admin" -H "Content-Type: application/json")

# After `dataagent demo`, export PROJECT_ID from its JSON output.
PROJECT_ID=${PROJECT_ID:?set PROJECT_ID}

curl -s "$BASE/readyz" | tee /tmp/ready.json
curl -s "${H[@]}" "$BASE/v1/projects/$PROJECT_ID/agents" | tee /tmp/agents.json
curl -s "${H[@]}" "$BASE/v1/projects/$PROJECT_ID/analytics/metrics?metric=session_volume&start=2024-01-01&end=2024-12-31"
