# Data Agent

**言語 / Languages:** [English](README.md) · [中文](README.zh.md) · **日本語** · [Deutsch](README.de.md)

**本番 Agent の利用状況から、監査可能なリリース判断へ。**

マルチテナント基盤です。本番 Agent のセッション／利用量／フィードバックを収集し、日次指標とチャートを生成し、シード Q&A とガバナンスされたトラフィックから版付きベンチマークを維持し、候補リリースを現行本番版と比較します。

<p align="center">
  <img src="docs/assets/architecture.png" alt="Data Agent architecture — From live agent usage to release decisions" width="100%" />
</p>

<p align="center"><em>From live agent usage to release decisions — マルチテナント分析、進化するベンチマーク、リリース前評価</em></p>

---

## 目次

1. [解決する課題](#1-解決する課題)
2. [システムの動き](#2-システムの動き)
3. [5 分で起動](#3-5-分で起動)
4. [コア概念](#4-コア概念)
5. [役割別の始め方](#5-役割別の始め方)
6. [SDK で本番トラフィックを取り込む](#6-sdk-で本番トラフィックを取り込む)
7. [日次分析とチャート](#7-日次分析とチャート)
8. [ベンチマーク：取り込み・更新・公開](#8-ベンチマーク取り込み更新公開)
9. [リリース前実験](#9-リリース前実験)
10. [権限とモジュールフラグ](#10-権限とモジュールフラグ)
11. [HTTP API 早見表](#11-http-api-早見表)
12. [デプロイと移行](#12-デプロイと移行)
13. [設定とトラブルシューティング](#13-設定とトラブルシューティング)
14. [関連ドキュメント](#14-関連ドキュメント)

---

## 1. 解決する課題

| 課題 | Data Agent の対応 |
|------|-------------------|
| 複数 Agent／チームで観測を共有すると権限が漏れる | **Tenant → Project → Agent → Release** の硬い境界。ロールは Agent／グループ／プロジェクト全体に紐づく |
| テレメトリ形式がバラバラ（SDK、OTLP、レガシー JSON） | 正規イベントへ統一。OTLP と版付き宣言的 JSON マッピング（dry-run 付き） |
| 指標定義のドリフト／Agent 横断の二重計上 | 指標レジストリ＋プロジェクト TZ の日境界。**認可された Agent を先に絞ってから集計** |
| 手作業ベンチマークでリリース間比較ができない | シード QA → 日次候補 → レビュー → 公開。過去 manifest は再実行可能 |
| 「体感で良くなった」リリース判断 | 同一 `benchmark_version` で baseline vs candidate。ルール＋ Azure OpenAI 判定。ゲートはナラティブと独立 |

**原則:** 取り込みからレポートまで同じアクセス規則。デフォルト拒否。機微フィールド（コスト、原文、holdout、判定詳細）は明示的な付与が必要。

---

## 2. システムの動き

図の左から右へ:

```
本番 Agent / フィードバック / 既存システム / シード QA
        │
        ▼
   取り込みと検証（認証 · 正規化 · 重複排除 · 秘匿化）
        │
        ├─► 日次分析（DAU/MAU、評価、コスト、ツール／スキル順位）
        ├─► ベンチマーク更新（候補 → レビュー → core / rolling / regression 公開）
        └─► リリース前評価（baseline vs candidate · 採点 · 回帰ゲート）
                │
                ▼
     ビジネス向けダッシュボード · Admin/レビュアー · 開発者 · SDK / HTTP API
```

- **ストレージとジョブ:** PostgreSQL がイベント、ベンチマーク、権限、ジョブを保持。Worker はリースと fencing token でジョブを取得し、中断後も再開可能。
- **LLM 呼び出し:** 本システムの判定／レポート叙述はすべて **Azure OpenAI**（ローカル既定は `fake` トランスポートで実キー不要）。
- **デプロイ:** Phase 1 は AKS で API＋Worker。Phase 2 は Worker を Alibaba Cloud Agent Sandbox へ。**同一イメージ digest**。DB／シークレット／安定 API／スケジューラは Sandbox の外。

---

## 3. 5 分で起動

### 前提

- Python **3.12+**
- PostgreSQL **16+**（ローカルまたは Docker）

### Docker Compose（推奨）

```bash
git clone https://github.com/John-Maximilian-Liu/Data-Agent.git
cd Data-Agent
docker compose up --build
# API: http://127.0.0.1:8000
# Health: curl http://127.0.0.1:8000/readyz
```

### ローカル開発

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

`demo` の JSON から `project_id` / `agents` / `experiment_id` を控えてください。

### ヘルスとドキュメント

| URL | 用途 |
|-----|------|
| `GET /healthz` | Liveness |
| `GET /readyz` | Readiness（スキーマ確認含む） |
| `GET /docs` | Swagger UI |
| `GET /v1/meta` | システムロールと既定モジュール |

オフライン認証:

```http
X-Dataagent-Principal: demo-admin
```

---

## 4. コア概念

### 階層

| 層 | 意味 |
|----|------|
| **Tenant** | 組織の硬い境界。テナント横断共有なし |
| **Project** | タイムゾーン、モジュール、ソース、ベンチマークを持つワークスペース |
| **Agent** | 観測／評価対象。`active` / `paused` / `archived` |
| **Release** | Agent の安定版。テスト endpoint または CI ターゲットを登録 |
| **Agent Group** | プロジェクト内の集合。**スコープ選択子のみ**で、単独では権限を与えない |

### 3 つのワークフロー

1. **日次分析** — セッション量、完了率、ビジネス成功、評価、DAU/MAU、トークン／コスト、Top KB／ツール／スキル、遅延分位  
2. **ベンチマーク更新** — シード QA 取り込み → 本番サンプリング → レビュー → 公開  
3. **リリース前評価** — `benchmark_version` ＋ baseline/candidate → 実行 → 採点 → ゲート

### 取り込み経路

| 経路 | 用途 |
|------|------|
| `POST .../events:batch` | Python SDK／独自コレクタ |
| `POST .../otlp/v1/traces` | OpenTelemetry OTLP/HTTP |
| `POST .../sources/{id}/webhook` | レガシー JSON＋版付きマッピング |
| `POST .../feedback` | 評価／結果の改訂 |

必須フィールド: `event_id`, `event_type`, `schema_version`, `occurred_at`, `payload`。

---

## 5. 役割別の始め方

| 役割 | 最初の一歩 |
|------|------------|
| **Platform / Project Admin** | Agent・Source 登録 → 資格情報発行 → モジュールとロールバインド → refresh 実行 |
| **Benchmark Reviewer** | 候補一覧 → 承認／却下（revision に紐づけ）→ holdout を漏らさない |
| **Agent Developer** | Release 登録 → Experiment 開始 → 認可された Agent の結果／レポートを読む |
| **Analyst / Viewer** | `/analytics/metrics` と charts。コスト等は追加権限が必要 |
| **Service Account** | Source／CI 資格情報のみでイベント書き込みまたは CI 結果送信 |

```bash
export BASE=http://127.0.0.1:8000
export PROJECT_ID=<demo の project_id>
export H='-H X-Dataagent-Principal:demo-admin -H Content-Type:application/json'

curl -s $H -X POST "$BASE/v1/projects/$PROJECT_ID/agents" \
  -d '{"slug":"support-bot","name":"Support Bot","owner_team":"cx"}'
```

有効権限の確認:

```bash
curl -s $H "$BASE/v1/projects/$PROJECT_ID/access/effective"
```

---

## 6. SDK で本番トラフィックを取り込む

SDK は `httpx` と `pydantic` のみに依存し、サーバパッケージなしでインストールできます。

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

### バッチ結果の意味

| 状態 | 意味 |
|------|------|
| `accepted` | コミット済み |
| `duplicate` | 同一 scope/source/`event_id` かつ同一内容ハッシュ（再送安全） |
| `content_conflict` | 同一 `event_id` で内容が異なる |
| `rejected` | 未書き込み。認可外 Agent の有無は**漏らさない** |

ツール化:

```python
tools = client.as_tools(project_id)
```

詳細: [`examples/sdk_quickstart.py`](examples/sdk_quickstart.py)。

---

## 7. 日次分析とチャート

```bash
dataagent enqueue-daily --date 2024-06-01

curl -s $H "$BASE/v1/projects/$PROJECT_ID/analytics/metrics\
?metric=session_volume&start=2024-06-01&end=2024-06-07"

curl -s $H "$BASE/v1/projects/$PROJECT_ID/analytics/charts/session_volume\
?start=2024-06-01&end=2024-06-07"
```

主な指標: `session_volume` · `completion_rate` · `success_rate` · `ratings` · `dau` · `mau` · `tokens` · `cost` · `top_kb` · `top_tools` · `top_skills` · `latency`

定義: [`docs/ANALYTICS.md`](docs/ANALYTICS.md)。

---

## 8. ベンチマーク：取り込み・更新・公開

### ① シード QA 取り込み（→ v1）

必須: `question`, `expected_answer`（CSV / JSONL）。

```bash
curl -s $H -X POST "$BASE/v1/projects/$PROJECT_ID/benchmarks" \
  -d '{"slug":"core","name":"Core QA","ownership":"agent_scoped","owner_agent_id":"<agent>"}'

curl -s $H -X POST "$BASE/v1/projects/$PROJECT_ID/benchmarks/$BENCH_ID/qa-imports" \
  -d "$(jq -n --rawfile c fixtures/qa/seed.jsonl \
    '{content:$c, format:"jsonl", validate_only:false, suite:"fixed_core"}')"
```

失敗時は **行番号＋フィールド＋エラーコード**。初回公開で **v1**。

### ② 本番からの refresh（Admin）

```bash
curl -s $H -X POST "$BASE/v1/projects/$PROJECT_ID/benchmarks/$BENCH_ID/refreshes" \
  -d '{"dry_run":false}'
# → 202 + job_id
```

本番トラフィック単独では expected answer を確定できません。低評価はレビュー証拠になります。

### ③ レビューと新バージョン公開

```bash
curl -s $H -X POST \
  "$BASE/v1/projects/$PROJECT_ID/benchmarks/$BENCH_ID/candidates/$CAND_ID/review" \
  -d '{"decision":"approved","expected_revision":1,"expected_answer":"Paris"}'

curl -s $H -X POST "$BASE/v1/projects/$PROJECT_ID/benchmarks/$BENCH_ID/versions"
```

スイート: `fixed_core` · `rolling_production` · `regression`。

---

## 9. リリース前実験

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

結果:

```bash
curl -s $H "$BASE/v1/projects/$PROJECT_ID/experiments/$EXP_ID/report"
```

### ゲートの読み方

- 品質／信頼性／遅延／コストは**独立**判定  
- 全体: `pass` | `fail` | `inconclusive`  
- 重要証拠欠如やペア不足 → `inconclusive`  
- **欠落 trace ≠ 0 点**（missing）  
- Azure ナラティブはゲート結果を**変えない**  

---

## 10. 権限とモジュールフラグ

| ロール | 概要 |
|--------|------|
| Tenant Admin | プロジェクト作成と委任。原文／コストは別データ権限 |
| Project Admin | Agent／Source／モジュール／グループ／委任／refresh |
| Agent Owner | 割り当て Agent の設定と試走 |
| Agent Developer | 指標／trace、実験の実行・確認 |
| Benchmark Reviewer | 許可されたデータセット／候補のレビュー |
| Analyst / Viewer | 認可済み集計の読み取り |
| Service Account | 紐づいた `source.write` / `ci.submit` のみ |

プロジェクトバインドは **いずれか 1 つ**: 単一 Agent · Agent Group · `whole_project=true`。  
「Agent A のコスト権」と「Agent B の trace 権」を組み合わせて B のコストを出すことは禁止です。

```bash
curl -s $H -X PATCH "$BASE/v1/projects/$PROJECT_ID/settings" \
  -d '{"modules":{"cost":false,"reports":true}}'
```

---

## 11. HTTP API 早見表

プレフィックス: `/v1/projects/{project_id}`

| 系統 | パス | 用途 |
|------|------|------|
| カタログ | `/agents` `/agent-groups` | 登録・一時停止・アーカイブ・メンバー |
| 権限 | `/roles` `/memberships` `/role-bindings` `/access/effective` | 付与と自己確認 |
| ソース | `/sources` `.../mapping-revisions` `.../credentials` | マッピング／資格情報 |
| 取り込み | `/events:batch` `/feedback` `/otlp/v1/traces` `.../webhook` | 3 経路 |
| 分析 | `/analytics/metrics` `/analytics/charts/{metric}` | 表と Plotly JSON |
| ベンチマーク | `/benchmarks` ほか | ライフサイクル |
| 実験 | `/experiments` ほか | リリース比較 |
| ジョブ | `/jobs/{id}` | 202 非同期のポーリング |

非同期作成は **HTTP 202**＋`job_id`。再試行可能な書き込みは `Idempotency-Key`。OpenAPI は `/docs`。

---

## 12. デプロイと移行

| フェーズ | 内容 |
|----------|------|
| **Phase 1** | AKS: `deploy/aks/` — API／Worker、CronJob、migrate Job。Azure OpenAI は Entra Workload ID |
| **Phase 2** | Worker を Alibaba Cloud Agent Sandbox へ: `deploy/alibaba-sandbox/` — **同一 digest** |

```bash
docker build -t dataagent:local .
docker run --rm dataagent:local smoke
```

中断／置換ドリル: [`docs/MIGRATION_DRILL.md`](docs/MIGRATION_DRILL.md)。

---

## 13. 設定とトラブルシューティング

| 変数 | 意味 |
|------|------|
| `DATAAGENT_DATABASE_URL` | `postgresql+psycopg://...` |
| `DATAAGENT_AUTH_MODE` | `offline`（開発）／`oidc`（本番） |
| `DATAAGENT_AZURE_TRANSPORT` | `fake` \| `live` |
| `DATAAGENT_HMAC_PEPPER` | ユーザー仮名化 HMAC（コミット禁止） |
| `DATAAGENT_WORKER_ID` | Worker インスタンス名 |

| 症状 | 対処 |
|------|------|
| `schema_missing` | 先に `dataagent migrate` |
| `agent_not_permitted` | Source の `allowed_agent_ids` を確認／Agent 状態を確認 |
| `403 not_a_member` | プロジェクト membership を付与 |
| 実験が `queued` のまま | `dataagent worker` を起動 |

```bash
pytest -q -m "not live"
```

---

## 14. 関連ドキュメント

| ドキュメント | 内容 |
|--------------|------|
| [`AGENTS.md`](AGENTS.md) | コーディング Agent 向け規約 |
| [`docs/OPERATOR.md`](docs/OPERATOR.md) | 運用手順 |
| [`docs/ANALYTICS.md`](docs/ANALYTICS.md) | 指標定義 |
| [`docs/MIGRATION_DRILL.md`](docs/MIGRATION_DRILL.md) | Worker 置換と live check |
| [`fixtures/`](fixtures/) | サンプル |
| [`deploy/`](deploy/) | マニフェスト |

---

## パッケージ

| パッケージ | 役割 |
|------------|------|
| `dataagent` | FastAPI API、Worker、取り込み、分析、ベンチマーク、実験、認可、ジョブ |
| `dataagent-sdk` | 独立インストール可能なクライアント |

**CLI:** `dataagent api` · `worker` · `enqueue-daily` · `migrate` · `smoke` · `demo`

---

## License

Apache-2.0
