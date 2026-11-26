# Data Agent

**Sprachen / Languages:** [English](README.md) · [中文](README.zh.md) · [日本語](README.ja.md) · **Deutsch**

**Von Live-Agent-Nutzung zu prüfbaren Release-Entscheidungen.**

Multi-Tenant-Plattform: beobachtet Produktions-Agents, erzeugt Tageskennzahlen und Charts, pflegt versionierte Benchmarks aus Seed-Q&A und geregeltem Traffic und vergleicht Kandidaten-Releases mit der aktuellen Produktion.

<p align="center">
  <img src="docs/assets/architecture.png" alt="Data Agent architecture — From live agent usage to release decisions" width="100%" />
</p>

<p align="center"><em>From live agent usage to release decisions — Multi-Tenant-Analysen, wachsende Benchmarks und Pre-Release-Evaluation</em></p>

---

## Inhaltsverzeichnis

1. [Welche Probleme gelöst werden](#1-welche-probleme-gelöst-werden)
2. [So funktioniert das System](#2-so-funktioniert-das-system)
3. [In 5 Minuten startklar](#3-in-5-minuten-startklar)
4. [Kernkonzepte](#4-kernkonzepte)
5. [Einstieg nach Rolle](#5-einstieg-nach-rolle)
6. [Produktions-Traffic per SDK erfassen](#6-produktions-traffic-per-sdk-erfassen)
7. [Tagesanalysen und Charts](#7-tagesanalysen-und-charts)
8. [Benchmarks: Import, Refresh, Publish](#8-benchmarks-import-refresh-publish)
9. [Pre-Release-Experimente](#9-pre-release-experimente)
10. [Berechtigungen und Modul-Flags](#10-berechtigungen-und-modul-flags)
11. [HTTP-API-Kurzreferenz](#11-http-api-kurzreferenz)
12. [Deployment und Migration](#12-deployment-und-migration)
13. [Konfiguration und Fehlerbehebung](#13-konfiguration-und-fehlerbehebung)
14. [Weiterführende Dokumentation](#14-weiterführende-dokumentation)

---

## 1. Welche Probleme gelöst werden

| Problem | Was Data Agent tut |
|---------|--------------------|
| Gemeinsame Observability leakt Zugriffe zwischen Agents/Teams | Harte Grenze **Tenant → Project → Agent → Release**; Rollen an Agent, Gruppe oder Gesamtprojekt |
| Heterogene Telemetrie (SDK, OTLP, Legacy-JSON) | Kanonische Events; OTLP und versionierte deklarative JSON-Mappings mit Dry-Run |
| Driftende Metrikdefinitionen / Doppelzählung über Agents | Metrik-Registry + Projekt-Zeitzone; **erst autorisierte Agents filtern, dann aggregieren** |
| Handgebaute Benchmarks, nicht vergleichbar | Seed-QA → tägliche Kandidaten → Review → Publish; historische Manifeste bleiben lauffähig |
| „Fühlt sich besser an“-Releases | Dieselbe Benchmark-Version: Baseline vs. Kandidat; Regeln + Azure-OpenAI-Urteil; Gates unabhängig von Narrativ |

**Prinzip:** Zugriffsregeln gelten von Intake bis Report. Default Deny. Sensible Felder (Kosten, Rohtext, Holdout, Judge-Details) brauchen explizite Grants.

---

## 2. So funktioniert das System

Von links nach rechts im Diagramm:

```
Produktions-Agents / Feedback / Legacy-Systeme / Seed-QA
        │
        ▼
   Intake & Validierung (Auth · Normalisieren · Deduplizieren · Redaktion)
        │
        ├─► Tagesanalysen (DAU/MAU, Bewertungen, Kosten, Tool-/Skill-Ranking)
        ├─► Benchmark-Updates (Kandidaten → Review → Publish core / rolling / regression)
        └─► Pre-Release-Evaluation (Baseline vs. Kandidat · Scoring · Regressions-Gates)
                │
                ▼
     Business-Dashboards · Admins/Reviewer · Entwickler · SDK / HTTP-API
```

- **Speicher & Jobs:** PostgreSQL speichert Events, Benchmarks, Berechtigungen und Jobs. Worker beanspruchen Arbeit mit Leases und Fencing-Tokens; unterbrochene Arbeit ist wiederaufsetzbar.
- **LLM-Aufrufe:** Judge und Report-Narrativ in diesem System nutzen immer **Azure OpenAI** (lokal standardmäßig `fake`-Transport, kein echter Schlüssel nötig).
- **Deploy:** Phase 1: API + Worker auf AKS. Phase 2: Worker zu Alibaba Cloud Agent Sandbox — **derselbe Image-Digest**; DB, Secrets, stabile API und Scheduler bleiben außerhalb.

---

## 3. In 5 Minuten startklar

### Voraussetzungen

- Python **3.12+**
- PostgreSQL **16+** (lokal oder Docker)

### Docker Compose (empfohlen)

```bash
git clone https://github.com/John-Maximilian-Liu/Data-Agent.git
cd Data-Agent
docker compose up --build
# API: http://127.0.0.1:8000
# Health: curl http://127.0.0.1:8000/readyz
```

### Lokale Entwicklung

```bash
export DATAAGENT_DATABASE_URL=postgresql+psycopg://dataagent:dataagent@127.0.0.1:5432/dataagent
export DATAAGENT_AUTH_MODE=offline
export DATAAGENT_AZURE_TRANSPORT=fake
export DATAAGENT_ENVIRONMENT=development

pip install -e ".[dev]" -e "./packages/dataagent-sdk"
dataagent migrate
dataagent smoke      # schema_ok

dataagent api        # :8000
dataagent worker
dataagent demo
```

Notieren Sie aus der Demo-JSON: `project_id`, `agents`, `experiment_id`.

### Health und Docs

| URL | Zweck |
|-----|-------|
| `GET /healthz` | Liveness |
| `GET /readyz` | Readiness (inkl. Schema-Check) |
| `GET /docs` | Swagger UI |
| `GET /v1/meta` | Systemrollen und Standardmodule |

Offline-Auth:

```http
X-Dataagent-Principal: demo-admin
```

---

## 4. Kernkonzepte

### Hierarchie

| Ebene | Bedeutung |
|-------|-----------|
| **Tenant** | Harte Organisationsgrenze — kein Cross-Tenant-Sharing |
| **Project** | Workspace mit Zeitzone, Modul-Flags, Sources und Benchmarks |
| **Agent** | Beobachtete/evaluierte Anwendung; Status: `active` / `paused` / `archived` |
| **Release** | Stabile Agent-Version; Test-Endpoint oder CI-Ziel registrieren |
| **Agent Group** | Sammlung im Projekt — **nur Scope-Selektor**, vergibt allein keine Rechte |

### Drei Workflows

1. **Tagesanalysen** — Session-Volumen, Completion, Business Success, Ratings, DAU/MAU, Tokens/Kosten, Top KB/Tools/Skills, Latenz-Perzentile  
2. **Benchmark-Updates** — Seed-QA → Produktionssampling → Review → Publish  
3. **Pre-Release-Evaluation** — `benchmark_version` + Baseline/Kandidat → Ausführen → Scoren → Gate

### Intake-Pfade

| Pfad | Wann |
|------|------|
| `POST .../events:batch` | Python-SDK / eigene Collector |
| `POST .../otlp/v1/traces` | OpenTelemetry OTLP/HTTP |
| `POST .../sources/{id}/webhook` | Legacy-JSON + versioniertes Mapping |
| `POST .../feedback` | Rating-/Outcome-Revisionen |

Pflichtfelder: `event_id`, `event_type`, `schema_version`, `occurred_at`, `payload`.

---

## 5. Einstieg nach Rolle

| Rolle | Erste Schritte |
|-------|----------------|
| **Platform / Project Admin** | Agents & Sources anlegen → Credentials → Module & Role Bindings → Refresh auslösen |
| **Benchmark Reviewer** | Kandidaten listen → freigeben/ablehnen (an Revision gebunden) → Holdout nicht leaken |
| **Agent Developer** | Releases registrieren → Experiment starten → Ergebnisse/Reports nur für autorisierte Agents |
| **Analyst / Viewer** | `/analytics/metrics` und Charts; Kosten u. a. brauchen Zusatzrechte |
| **Service Account** | Nur Source-/CI-Credentials für Events oder CI-Ergebnisse |

```bash
export BASE=http://127.0.0.1:8000
export PROJECT_ID=<project_id aus der Demo>
export H='-H X-Dataagent-Principal:demo-admin -H Content-Type:application/json'

curl -s $H -X POST "$BASE/v1/projects/$PROJECT_ID/agents" \
  -d '{"slug":"support-bot","name":"Support Bot","owner_team":"cx"}'
```

Effektive Rechte prüfen:

```bash
curl -s $H "$BASE/v1/projects/$PROJECT_ID/access/effective"
```

---

## 6. Produktions-Traffic per SDK erfassen

Das SDK hängt nur von `httpx` und `pydantic` ab — ohne Server-Paket installierbar.

```bash
pip install -e ./packages/dataagent-sdk
```

```python
from dataagent_sdk import Client

client = Client(
    base_url="http://127.0.0.1:8000",
    principal="demo-admin",
)

result = client.events.batch(
    project_id,
    source_id=source_id,
    events=[...],
    idempotency_key="batch-2024-06-01-1",
)
```

### Batch-Semantik

| Status | Bedeutung |
|--------|-----------|
| `accepted` | Committet |
| `duplicate` | Gleicher Scope/Source/`event_id` und Content-Hash (replay-sicher) |
| `content_conflict` | Gleiche `event_id`, anderer Inhalt |
| `rejected` | Nicht geschrieben; **leakt nicht**, ob der Agent außerhalb des Grants existiert |

Als Tools bereitstellen:

```python
tools = client.as_tools(project_id)
```

Mehr: [`examples/sdk_quickstart.py`](examples/sdk_quickstart.py).

---

## 7. Tagesanalysen und Charts

```bash
dataagent enqueue-daily --date 2024-06-01

curl -s $H "$BASE/v1/projects/$PROJECT_ID/analytics/metrics\
?metric=session_volume&start=2024-06-01&end=2024-06-07"

curl -s $H "$BASE/v1/projects/$PROJECT_ID/analytics/charts/session_volume\
?start=2024-06-01&end=2024-06-07"
```

Wichtige Metriken: `session_volume` · `completion_rate` · `success_rate` · `ratings` · `dau` · `mau` · `tokens` · `cost` · `top_kb` · `top_tools` · `top_skills` · `latency`

Definitionen: [`docs/ANALYTICS.md`](docs/ANALYTICS.md).

---

## 8. Benchmarks: Import, Refresh, Publish

### ① Seed-QA importieren (→ v1)

Pflicht: `question`, `expected_answer` (CSV / JSONL).

```bash
curl -s $H -X POST "$BASE/v1/projects/$PROJECT_ID/benchmarks" \
  -d '{"slug":"core","name":"Core QA","ownership":"agent_scoped","owner_agent_id":"<agent>"}'

curl -s $H -X POST "$BASE/v1/projects/$PROJECT_ID/benchmarks/$BENCH_ID/qa-imports" \
  -d "$(jq -n --rawfile c fixtures/qa/seed.jsonl \
    '{content:$c, format:"jsonl", validate_only:false, suite:"fixed_core"}')"
```

Fehler liefern **Zeile + Feld + Fehlercode**. Erster Publish erzeugt **v1**.

### ② Refresh aus Produktion (Admin)

```bash
curl -s $H -X POST "$BASE/v1/projects/$PROJECT_ID/benchmarks/$BENCH_ID/refreshes" \
  -d '{"dry_run":false}'
# → 202 + job_id
```

Produktions-Traffic darf die eigene Expected Answer nicht allein zertifizieren. Niedrige Ratings werden Review-Evidenz.

### ③ Review und neue Version

```bash
curl -s $H -X POST \
  "$BASE/v1/projects/$PROJECT_ID/benchmarks/$BENCH_ID/candidates/$CAND_ID/review" \
  -d '{"decision":"approved","expected_revision":1,"expected_answer":"Paris"}'

curl -s $H -X POST "$BASE/v1/projects/$PROJECT_ID/benchmarks/$BENCH_ID/versions"
```

Suites: `fixed_core` · `rolling_production` · `regression`.

---

## 9. Pre-Release-Experimente

```bash
curl -s $H -X POST "$BASE/v1/projects/$PROJECT_ID/releases" \
  -d '{"agent_id":"<agent>","version":"1.0.0","is_production":true}'

curl -s $H -X POST "$BASE/v1/projects/$PROJECT_ID/experiments" \
  -d '{
    "agent_id":"<agent>",
    "benchmark_version_id":"<version-uuid>",
    "baseline_release_id":"<baseline>",
    "candidate_release_id":"<candidate>",
    "suites":["fixed_core","regression"],
    "evaluators":["task_success","rag","tools","skills","observable_path","prompt_behavior","runtime_cost"]
  }'
```

Ergebnisse:

```bash
curl -s $H "$BASE/v1/projects/$PROJECT_ID/experiments/$EXP_ID/report"
```

### Gates lesen

- Qualität / Zuverlässigkeit / Latenz / Kosten werden **unabhängig** bewertet  
- Gesamt: `pass` | `fail` | `inconclusive`  
- Fehlende kritische Evidenz oder zu wenige Paare → `inconclusive`  
- **Fehlende Traces ≠ Null-Scores** (als missing)  
- Azure-Narrativ ändert Gates **nicht**  

---

## 10. Berechtigungen und Modul-Flags

| Rolle | Kurzbeschreibung |
|-------|------------------|
| Tenant Admin | Projekte anlegen und delegieren; Rohtext/Kosten brauchen eigene Datenrechte |
| Project Admin | Agents/Sources/Module/Gruppen/Delegation/Refresh |
| Agent Owner | Konfiguration und Testläufe für zugewiesene Agents |
| Agent Developer | Metriken/Traces; Experimente ausführen/prüfen |
| Benchmark Reviewer | Freigegebene Datensätze/Kandidaten reviewen |
| Analyst / Viewer | Autorisierte Aggregate lesen |
| Service Account | Nur gebundenes `source.write` / `ci.submit` |

Projekt-Binding-Selektor (**genau einer**): einzelner Agent · Agent Group · `whole_project=true`.  
**Nie** „Kosten auf Agent A“ mit „Traces auf Agent B“ kombinieren, um Kosten von B sichtbar zu machen.

```bash
curl -s $H -X PATCH "$BASE/v1/projects/$PROJECT_ID/settings" \
  -d '{"modules":{"cost":false,"reports":true}}'
```

---

## 11. HTTP-API-Kurzreferenz

Präfix: `/v1/projects/{project_id}`

| Familie | Pfade | Zweck |
|---------|-------|-------|
| Katalog | `/agents` `/agent-groups` | Registrieren, pausieren, archivieren, Mitglieder |
| Zugriff | `/roles` `/memberships` `/role-bindings` `/access/effective` | Grants und Selbstprüfung |
| Sources | `/sources` `.../mapping-revisions` `.../credentials` | Mapping / Credentials |
| Intake | `/events:batch` `/feedback` `/otlp/v1/traces` `.../webhook` | Drei Intake-Pfade |
| Analytics | `/analytics/metrics` `/analytics/charts/{metric}` | Tabellen und Plotly-JSON |
| Benchmarks | `/benchmarks` usw. | Lebenszyklus |
| Experimente | `/experiments` usw. | Release-Vergleich |
| Jobs | `/jobs/{id}` | Async-202 pollen |

Async-Erzeugung → **HTTP 202** + `job_id`. Wiederholbare Writes: `Idempotency-Key`. OpenAPI: `/docs`.

---

## 12. Deployment und Migration

| Phase | Inhalt |
|-------|--------|
| **Phase 1** | AKS: `deploy/aks/` — API/Worker, CronJob, migrate Job; Azure OpenAI via Entra Workload ID |
| **Phase 2** | Worker zu Alibaba Cloud Agent Sandbox: `deploy/alibaba-sandbox/` — **gleicher Digest** |

```bash
docker build -t dataagent:local .
docker run --rm dataagent:local smoke
```

Unterbrechungs-/Ersatz-Drill: [`docs/MIGRATION_DRILL.md`](docs/MIGRATION_DRILL.md).

---

## 13. Konfiguration und Fehlerbehebung

| Variable | Bedeutung |
|----------|-----------|
| `DATAAGENT_DATABASE_URL` | `postgresql+psycopg://...` |
| `DATAAGENT_AUTH_MODE` | `offline` (Dev) / `oidc` (Prod) |
| `DATAAGENT_AZURE_TRANSPORT` | `fake` \| `live` |
| `DATAAGENT_HMAC_PEPPER` | HMAC für Pseudonyme (nie committen) |
| `DATAAGENT_WORKER_ID` | Worker-Instanzname |

| Symptom | Fix |
|---------|-----|
| `schema_missing` | Zuerst `dataagent migrate` |
| `agent_not_permitted` | `allowed_agent_ids` / Agent-Status prüfen |
| `403 not_a_member` | Project-Membership vergeben |
| Experiment bleibt `queued` | `dataagent worker` starten |

```bash
pytest -q -m "not live"
```

---

## 14. Weiterführende Dokumentation

| Dokument | Inhalt |
|----------|--------|
| [`AGENTS.md`](AGENTS.md) | Konventionen für Coding-Agents |
| [`docs/OPERATOR.md`](docs/OPERATOR.md) | Betriebshandbuch |
| [`docs/ANALYTICS.md`](docs/ANALYTICS.md) | Metrikdefinitionen |
| [`docs/MIGRATION_DRILL.md`](docs/MIGRATION_DRILL.md) | Worker-Ersatz und Live-Checks |
| [`fixtures/`](fixtures/) | Beispiele |
| [`deploy/`](deploy/) | Manifeste |

---

## Pakete

| Paket | Rolle |
|-------|-------|
| `dataagent` | FastAPI-API, Worker, Intake, Analytics, Benchmarks, Experimente, Auth, Jobs |
| `dataagent-sdk` | Unabhängig installierbarer Client |

**CLI:** `dataagent api` · `worker` · `enqueue-daily` · `migrate` · `smoke` · `demo`

---

## License

Apache-2.0
