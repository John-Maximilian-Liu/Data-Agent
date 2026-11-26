# Data Agent

**语言 / Languages:** [English](README.md) · **中文** · [日本語](README.ja.md) · [Deutsch](README.de.md)

**从线上 Agent 用量，到可审计的发版决策。**

多租户平台：采集生产 Agent 的会话 / 用量 / 反馈，生成每日指标与图表，用种子问答与治理后的流量维护版本化基准集，并在发版前对比候选版本与当前生产版本。

<p align="center">
  <img src="docs/assets/architecture.png" alt="Data Agent architecture — From live agent usage to release decisions" width="100%" />
</p>

<p align="center"><em>From live agent usage to release decisions — 多租户分析、演进中的基准集、发版前评估</em></p>

---

## 目录

1. [它解决什么问题](#1-它解决什么问题)
2. [系统怎么工作](#2-系统怎么工作)
3. [5 分钟本地跑通](#3-5-分钟本地跑通)
4. [核心概念（必读）](#4-核心概念必读)
5. [角色怎么上手](#5-角色怎么上手)
6. [用 SDK 接入生产流量](#6-用-sdk-接入生产流量)
7. [每日分析与图表](#7-每日分析与图表)
8. [基准集：导入、刷新、发版](#8-基准集导入刷新发版)
9. [发版前实验](#9-发版前实验)
10. [权限与模块开关](#10-权限与模块开关)
11. [HTTP API 速查](#11-http-api-速查)
12. [部署与迁移](#12-部署与迁移)
13. [配置与排障](#13-配置与排障)
14. [进一步阅读](#14-进一步阅读)

---

## 1. 它解决什么问题

| 痛点 | Data Agent 怎么做 |
|------|-------------------|
| 多个 Agent / 团队共用一套观测，权限容易串 | **Tenant → Project → Agent → Release** 硬边界；角色绑定到具体 Agent / 组 / 全项目 |
| 埋点格式不一（SDK、OTLP、旧系统 JSON） | 统一成规范事件；OTLP 与声明式 JSON 映射（带版本与 dry-run） |
| 指标口径漂、跨 Agent 重复计数 | 指标注册表 + 项目时区日切；**先按授权 Agent 过滤再聚合** |
| 基准集靠手工、发版前后不可比 | 种子 QA → 日更候选 → 评审发布；历史 manifest 可复跑 |
| 候选版本「感觉更好」无法证明 | 同基准版本上 baseline vs candidate；规则 + Azure OpenAI 裁判；门禁与叙述解耦 |

**设计原则（贯穿全链路）：** 访问规则从摄入到报表一致生效；默认拒绝；敏感字段（成本、原文、holdout、裁判细节）需显式授权。

---

## 2. 系统怎么工作

上图从左到右：

```
生产 Agent / 反馈 / 旧系统 / 种子 QA
        │
        ▼
   摄入与校验（鉴权 · 规范化 · 去重 · 脱敏）
        │
        ├─► 每日分析（DAU/MAU、评分、成本、工具/技能排行）
        ├─► 基准更新（候选 → 评审 → 发布 core / rolling / regression）
        └─► 发版前评估（基线 vs 候选 · 评分 · 回归门禁）
                │
                ▼
     业务看板 · Admin/评审 · 研发对比 · SDK / HTTP API
```

- **存储与任务**：PostgreSQL 持久化事件、基准、权限与作业；Worker 用租约 + fencing token 认领任务，可中断恢复。
- **模型调用**：本系统自身的裁判 / 报告叙述一律走 **Azure OpenAI**（本地默认可切 `fake` 传输，无需真实密钥）。
- **部署**：Phase 1 在 AKS 跑 API + Worker；Phase 2 可将 Worker 迁到阿里云 Agent Sandbox，**同一镜像 digest**，库与调度留在外部。

---

## 3. 5 分钟本地跑通

### 前置

- Python **3.12+**
- PostgreSQL **16+**（本机或 Docker）

### 一键 Compose（推荐）

```bash
git clone https://github.com/John-Maximilian-Liu/Data-Agent.git
cd Data-Agent
docker compose up --build
# API: http://127.0.0.1:8000
# 健康检查: curl http://127.0.0.1:8000/readyz
```

### 本机开发安装

```bash
# 1) 数据库
export DATAAGENT_DATABASE_URL=postgresql+psycopg://dataagent:dataagent@127.0.0.1:5432/dataagent
export DATAAGENT_AUTH_MODE=offline          # 仅开发；production 环境会拒绝
export DATAAGENT_AZURE_TRANSPORT=fake       # 离线假 Azure，无需真实密钥
export DATAAGENT_ENVIRONMENT=development

# 2) 安装
pip install -e ".[dev]" -e "./packages/dataagent-sdk"

# 3) 迁移 + 冒烟
dataagent migrate    # 或: alembic upgrade head
dataagent smoke      # 应输出 schema_ok

# 4) 启动（两个终端）
dataagent api        # :8000
dataagent worker

# 5) 离线演示：种子租户/Agent、摄入、基准 v1、跑实验
dataagent demo
```

`demo` 会打印 JSON，记下其中的 `project_id`、`agents`、`experiment_id`，后面示例都会用到。

### 打开文档与健康检查

| 地址 | 说明 |
|------|------|
| `GET /healthz` | 存活 |
| `GET /readyz` | 就绪（含 schema 检查） |
| `GET /docs` | Swagger UI（OpenAPI） |
| `GET /v1/meta` | 系统角色与默认模块 |

离线模式用请求头模拟登录用户：

```http
X-Dataagent-Principal: demo-admin
```

---

## 4. 核心概念（必读）

### 层级

| 层级 | 含义 |
|------|------|
| **Tenant** | 组织硬边界，不可跨租户共享数据 |
| **Project** | 工作空间；拥有时区、模块开关、来源与基准 |
| **Agent** | 被观测/评估的应用；状态：`active` / `paused` / `archived` |
| **Release** | Agent 的稳定版本；登记测试 endpoint 或 CI 目标 |
| **Agent Group** | 项目内集合，**只作授权选择器**，本身不赋权 |

### 三条工作流

1. **每日分析** — 会话量、完成率、业务成功率、评分、DAU/MAU、Token/成本、Top KB/工具/技能、延迟分位  
2. **基准更新** — 种子 QA 导入 → 生产流量采样候选 → 评审 → 发布版本（fixed core / rolling production / regression）  
3. **发版前评估** — 选定 `benchmark_version` + baseline/candidate release → 执行 → 评分 → 门禁（pass / fail / inconclusive）

### 摄入路径

| 路径 | 适用 |
|------|------|
| 规范事件 `POST .../events:batch` | Python SDK / 自建采集 |
| `POST .../otlp/v1/traces` | OpenTelemetry OTLP/HTTP |
| `POST .../sources/{id}/webhook` | 旧系统 JSON + 版本化 mapping |
| `POST .../feedback` | 评分与结果修订 |

每条事件需含：`event_id`、`event_type`、`schema_version`、`occurred_at`、`payload`。服务端盖 `received_at`，并从凭证推导租户 / 项目 / 来源。

---

## 5. 角色怎么上手

| 你是谁 | 典型第一步 |
|--------|------------|
| **平台 / 项目 Admin** | 建 Agent 与 Source → 发凭证 → 配模块与角色绑定 → 触发基准 refresh |
| **Benchmark Reviewer** | 看候选列表 → 批准/拒绝（绑定具体 revision）→ 不泄露 holdout |
| **Agent Developer** | 登记 Release → 开 Experiment → 读结果与报告（仅授权 Agent） |
| **Analyst / Viewer** | 查 `/analytics/metrics` 与 charts；成本等字段需额外授权 |
| **Service Account** | 只用 Source / CI 凭证写事件或提交私有 CI 结果 |

**示例：Admin 注册 Agent（离线）**

```bash
export BASE=http://127.0.0.1:8000
export PROJECT_ID=<demo 输出的 project_id>
export H='-H X-Dataagent-Principal:demo-admin -H Content-Type:application/json'

curl -s $H -X POST "$BASE/v1/projects/$PROJECT_ID/agents" \
  -d '{"slug":"support-bot","name":"Support Bot","owner_team":"cx"}'
```

**示例：给分析师绑定「仅 Agent Alpha」的 viewer 权限**

```bash
curl -s $H -X POST "$BASE/v1/projects/$PROJECT_ID/role-bindings" \
  -d '{
    "principal_id":"<analyst-principal-uuid>",
    "role_name":"viewer",
    "agent_id":"<alpha-agent-uuid>",
    "reason":"read aggregates for alpha only"
  }'
```

查看自己的有效权限：

```bash
curl -s $H "$BASE/v1/projects/$PROJECT_ID/access/effective"
```

---

## 6. 用 SDK 接入生产流量

SDK **只依赖** `httpx` + `pydantic`，可单独安装，不必装服务端包。

```bash
pip install -e ./packages/dataagent-sdk
# 或发布后: pip install dataagent-sdk
```

### 最小摄入示例

```python
from dataagent_sdk import Client

client = Client(
    base_url="http://127.0.0.1:8000",
    principal="demo-admin",          # 离线开发
    # credential="da_xxx...",        # 生产：Source / Agent 凭证
)

project_id = "..."
source_id = "..."

events = [
    {
        "event_id": "evt-1001",
        "event_type": "session.start",
        "schema_version": "1.0",
        "occurred_at": "2024-06-01T03:30:00Z",
        "agent_id": "<agent-uuid>",
        "payload": {"session_id": "s-1", "user_id": "u-42"},
    },
    {
        "event_id": "evt-1002",
        "event_type": "session.end",
        "schema_version": "1.0",
        "occurred_at": "2024-06-01T03:45:00Z",
        "agent_id": "<agent-uuid>",
        "payload": {
            "session_id": "s-1",
            "state": "completed",
            "technical_completed": True,
            "business_success": True,
        },
    },
    {
        "event_id": "evt-1003",
        "event_type": "model.call",
        "schema_version": "1.0",
        "occurred_at": "2024-06-01T03:40:00Z",
        "agent_id": "<agent-uuid>",
        "payload": {
            "provider_call_id": "call-abc",
            "model": "gpt-4o",
            "input_tokens": 120,
            "output_tokens": 40,
            "usage_class": "production",
        },
    },
]

result = client.events.batch(
    project_id,
    source_id=source_id,
    events=events,
    idempotency_key="batch-2024-06-01-1",  # 可安全重试
)
print(result)
# {"accepted": 3, "duplicate": 0, "rejected": 0, "content_conflict": 0, ...}
```

### 支持的 `event_type`

`session.start` · `session.end` · `run.completed` · `feedback.revision` · `model.call` · `retrieval` · `tool.call` · `skill.execution` · `span`

### 批处理语义（很重要）

| 状态 | 含义 |
|------|------|
| `accepted` | 已提交并落库 |
| `duplicate` | 同 scope/source/`event_id` 且内容哈希相同（重放安全） |
| `content_conflict` | 同 `event_id` 但内容变了 |
| `rejected` | 未写入（如 Agent 不在 Source 授权列表）；**不泄露**「是否存在但无权」 |

### 把 SDK 当工具给 Agent 用

```python
tools = client.as_tools(project_id)
# 默认允许授权读；写操作（摄入、导入 QA、开实验）需显式 scope
for t in tools:
    print(t["name"], "mutation=" + str(t.get("mutation")))
```

更多：`examples/sdk_quickstart.py`、`packages/dataagent-sdk/README.md`。

---

## 7. 每日分析与图表

### 调度日指标

```bash
dataagent enqueue-daily --date 2024-06-01
# Worker 会认领 kind=analytics.daily 的作业
```

### 查询指标 / 图表

```bash
# 会话量
curl -s $H "$BASE/v1/projects/$PROJECT_ID/analytics/metrics\
?metric=session_volume&start=2024-06-01&end=2024-06-07"

# Plotly 兼容 JSON
curl -s $H "$BASE/v1/projects/$PROJECT_ID/analytics/charts/session_volume\
?start=2024-06-01&end=2024-06-07"
```

### 指标一览

| metric | 说明 |
|--------|------|
| `session_volume` | 按本地日的生产会话（含状态维度） |
| `completion_rate` / `success_rate` | 技术完成 vs 业务成功（分母仅「已知结局」） |
| `ratings` | 评分量表分离；同人同对象取最新有效修订 |
| `dau` / `mau` | 伪匿名用户去重；跨 Agent 查询会再次去重 |
| `tokens` / `cost` | 输入/输出/缓存；成本需 `cost` 字段权限 |
| `top_kb` / `top_tools` / `top_skills` | 稳定 ID 排序，并列按 ID 决胜 |
| `latency` | 端到端 run wall time 的 P50/P95 |

口径细节见 [`docs/ANALYTICS.md`](docs/ANALYTICS.md)。示例图表：[`fixtures/charts/session_volume.example.json`](fixtures/charts/session_volume.example.json)。

---

## 8. 基准集：导入、刷新、发版

### ① 创建基准并导入种子 QA（→ v1）

种子文件：CSV 或 JSONL，必填 `question`、`expected_answer`；可选 `external_case_id`、`language`、`task_category`、`rubric`、`document_labels`、`permitted_tools` 等。

```bash
# 先 validate_only
curl -s $H -X POST "$BASE/v1/projects/$PROJECT_ID/benchmarks" \
  -d '{"slug":"core","name":"Core QA","ownership":"agent_scoped","owner_agent_id":"<agent>"}'

curl -s $H -X POST "$BASE/v1/projects/$PROJECT_ID/benchmarks/$BENCH_ID/qa-imports" \
  -d "$(jq -n --rawfile c fixtures/qa/seed.jsonl \
    '{content:$c, format:"jsonl", validate_only:false, suite:"fixed_core"}')"
```

校验失败会返回 **行号 + 字段 + 错误码**；全部合法才原子提交。首次发布种子导入即 **v1**。

### ② 从生产流量刷新候选（Admin）

```bash
# dry-run 预览
curl -s $H -X POST "$BASE/v1/projects/$PROJECT_ID/benchmarks/$BENCH_ID/refreshes" \
  -d '{"dry_run":true}'

# 正式：返回 202 + job_id
curl -s $H -X POST "$BASE/v1/projects/$PROJECT_ID/benchmarks/$BENCH_ID/refreshes" \
  -d '{"dry_run":false}'
```

流水线会：按水位取样 → 分层/过采样失败 → 脱敏 → 生成候选（`proposed` / `needs_review`…）。**生产流量不能单独证明自己的 expected answer**；低分只会变成评审证据。

### ③ 评审并发布新版本

```bash
curl -s $H "$BASE/v1/projects/$PROJECT_ID/benchmarks/$BENCH_ID/candidates"

curl -s $H -X POST \
  "$BASE/v1/projects/$PROJECT_ID/benchmarks/$BENCH_ID/candidates/$CAND_ID/review" \
  -d '{"decision":"approved","expected_revision":1,"expected_answer":"Paris"}'

curl -s $H -X POST "$BASE/v1/projects/$PROJECT_ID/benchmarks/$BENCH_ID/versions"
# 返回 version + manifest_hash；旧版本仍可复跑
```

三个套件：`fixed_core` · `rolling_production` · `regression`。

---

## 9. 发版前实验

### 登记版本

```bash
curl -s $H -X POST "$BASE/v1/projects/$PROJECT_ID/releases" \
  -d '{"agent_id":"<agent>","version":"1.0.0","is_production":true}'

curl -s $H -X POST "$BASE/v1/projects/$PROJECT_ID/releases" \
  -d '{"agent_id":"<agent>","version":"1.1.0-rc1","endpoint_url":"https://test.example/v1/chat"}'
```

### 启动实验（202 + job）

```bash
curl -s $H -X POST "$BASE/v1/projects/$PROJECT_ID/experiments" \
  -d '{
    "agent_id":"<agent>",
    "benchmark_version_id":"<version-uuid>",
    "baseline_release_id":"<baseline-release>",
    "candidate_release_id":"<candidate-release>",
    "suites":["fixed_core","regression"],
    "evaluators":["task_success","rag","tools","skills","observable_path","prompt_behavior","runtime_cost"]
  }'
```

Worker 执行后可查询：

```bash
curl -s $H "$BASE/v1/projects/$PROJECT_ID/experiments/$EXP_ID"
curl -s $H "$BASE/v1/projects/$PROJECT_ID/experiments/$EXP_ID/results"
curl -s $H "$BASE/v1/projects/$PROJECT_ID/experiments/$EXP_ID/report"
```

### 门禁怎么读

- 质量 / 可靠性 / 延迟 / 成本 **独立** 判定  
- 总结果：`pass` | `fail` | `inconclusive`  
- 缺关键证据或配对不足 → `inconclusive`（除非已有硬失败）  
- **缺失 trace ≠ 0 分**（记为 missing）  
- 叙述文案由 Azure 生成，**不影响**门禁计算  

私有 CI 可在跑完自家 Agent 后，用 `ci.submit` 凭证向 `/experiments/{id}/ci-results` 提交结果（带 `submission_key` 去重）。

报告示例：[`fixtures/reports/experiment.example.json`](fixtures/reports/experiment.example.json)。

---

## 10. 权限与模块开关

### 系统角色（起点）

| 角色 | 能力摘要 |
|------|----------|
| Tenant Admin | 建项目、委托项目管理；**原文/成本等仍要另授** |
| Project Admin | Agent/Source/模块/组/委托/触发 refresh |
| Agent Owner | 所辖 Agent 配置与试跑，无自动升到项目/租户管理 |
| Agent Developer | 看指标/trace，跑/审实验 |
| Benchmark Reviewer | 审已授权数据集或 Agent 范围候选 |
| Analyst / Viewer | 读授权聚合；成本/下钻/原文/导出需显式字段权 |
| Service Account | 仅绑定资源上的 source.write / ci.submit 等 |

绑定选择器（项目级 **三选一**）：单个 Agent · Agent Group · `whole_project=true`。  
**禁止**把「Agent A 的成本权」和「Agent B 的 trace 权」拼出 B 的成本。

### 模块开关

项目可关：`sessions` `feedback` `cost` `activity` `kb` `tools` `skills` `benchmark` `experiments` `reports`。  
模块在 **通过范围鉴权之后** 再检查；可在生产文本采集关闭时仍做纯 QA 评估。

```bash
curl -s $H -X PATCH "$BASE/v1/projects/$PROJECT_ID/settings" \
  -d '{"modules":{"cost":false,"reports":true}}'
```

---

## 11. HTTP API 速查

所有业务路由前缀：`/v1/projects/{project_id}`

| 族 | 路径 | 用途 |
|----|------|------|
| 目录 | `/agents` `/agent-groups` | 注册、暂停、归档、组成员 |
| 权限 | `/roles` `/memberships` `/role-bindings` `/access/effective` | 授权与自查 |
| 来源 | `/sources` `/sources/{id}/mapping-revisions` `/sources/{id}/credentials` | 映射 dry-run/发布、凭证轮换 |
| 摄入 | `/events:batch` `/feedback` `/otlp/v1/traces` `/sources/{id}/webhook` | 三类入口 |
| 分析 | `/analytics/metrics` `/analytics/charts/{metric}` | 表与 Plotly JSON |
| 基准 | `/benchmarks` `.../qa-imports` `.../refreshes` `.../candidates` `.../versions` | 生命周期 |
| 实验 | `/experiments` `.../results` `.../report` `.../cancel` `.../ci-results` | 对比发版 |
| 作业 | `/jobs/{id}` | 轮询 202 异步任务 |
| 设置 | `/settings` | 模块与项目配置 |

约定：

- 异步创建 → **HTTP 202** + `job_id` + `status_url`  
- 可重试的写操作带 `Idempotency-Key`（按 project + operation 作用域）  
- 稳定错误码 + `X-Request-Id`  
- OpenAPI：运行中的 `/docs`

Shell 示例：[`examples/api_quickstart.sh`](examples/api_quickstart.sh)。

---

## 12. 部署与迁移

| 阶段 | 内容 |
|------|------|
| **Phase 1** | AKS：`deploy/aks/` — API Deployment、Worker Deployment、Service、日调度 CronJob、迁移 Job；Azure OpenAI 用 Entra Workload ID |
| **Phase 2** | Worker 迁阿里云 Agent Sandbox：`deploy/alibaba-sandbox/` — **同一镜像 digest**；DB / 密钥 / 稳定 API / 调度留在 Sandbox 外 |

```bash
# 构建单一镜像
docker build -t dataagent:local .
# 入口: api | worker | enqueue-daily | migrate | smoke
docker run --rm dataagent:local smoke
```

中断与替换演练（租约过期、fencing、回滚到 AKS Worker）见 [`docs/MIGRATION_DRILL.md`](docs/MIGRATION_DRILL.md)。

---

## 13. 配置与排障

### 常用环境变量

| 变量 | 说明 |
|------|------|
| `DATAAGENT_DATABASE_URL` | `postgresql+psycopg://...`（必须 Postgres） |
| `DATAAGENT_AUTH_MODE` | `offline`（开发）或 `oidc`（生产） |
| `DATAAGENT_ENVIRONMENT` | `development` / `test` / `production`（生产禁 offline） |
| `DATAAGENT_AZURE_TRANSPORT` | `fake` \| `live` |
| `DATAAGENT_AZURE_OPENAI_ENDPOINT` / `_DEPLOYMENT` | 直播时必填 |
| `DATAAGENT_AZURE_USE_ENTRA` | 默认 true；可改为 API Key 注入 |
| `DATAAGENT_HMAC_PEPPER` | 用户伪匿名 HMAC（勿提交仓库） |
| `DATAAGENT_WORKER_ID` | Worker 实例名（租约所有者） |

完整运维说明：[`docs/OPERATOR.md`](docs/OPERATOR.md)。依赖理由：[`docs/DEPENDENCIES.md`](docs/DEPENDENCIES.md)。

### 常见问题

| 现象 | 处理 |
|------|------|
| `readyz` → `schema_missing` | 先跑 `dataagent migrate` |
| 摄入 `rejected` / `agent_not_permitted` | Agent 未加入 Source 的 `allowed_agent_ids`，或 Agent 已 `paused`/`archived` |
| `403 not_a_member` | 主体缺少该项目（或租户级）membership |
| `idempotency_conflict` | 同一 Key 换了请求体；换 Key 或保持体一致 |
| 实验一直 `queued` | 确认 Worker 在跑：`dataagent worker` |
| 本地无 Azure 密钥 | 保持 `DATAAGENT_AZURE_TRANSPORT=fake` |

### 测试

```bash
export DATAAGENT_DATABASE_URL=postgresql+psycopg://dataagent:dataagent@127.0.0.1:5432/dataagent_test
pytest -q -m "not live"
# 含 1000 Agent 负载基线: pytest -q -m slow
```

---

## 14. 进一步阅读

| 文档 | 内容 |
|------|------|
| [`AGENTS.md`](AGENTS.md) | 给编码 Agent 的仓库约定 |
| [`docs/OPERATOR.md`](docs/OPERATOR.md) | 启动、采集、映射、回放、迁移 |
| [`docs/ANALYTICS.md`](docs/ANALYTICS.md) | 指标定义 |
| [`docs/MIGRATION_DRILL.md`](docs/MIGRATION_DRILL.md) | Worker 替换演练与 live check 清单 |
| [`docs/CLOSURE_SUMMARY.md`](docs/CLOSURE_SUMMARY.md) | 里程碑交付对照 |
| [`fixtures/`](fixtures/) | 事件、OTLP、QA、图表与报告样例 |
| [`deploy/`](deploy/) | AKS 与 Sandbox 清单 |

---

## 包结构

| 包 | 职责 |
|----|------|
| `dataagent` | FastAPI API、Worker、摄入、分析、基准、实验、鉴权、作业 |
| `dataagent-sdk` | 独立可安装客户端：`events` / `analytics` / `benchmarks` / `experiments` / `as_tools()` |

**CLI 入口：** `dataagent api` · `worker` · `enqueue-daily` · `migrate` · `smoke` · `demo`

---

## License

Apache-2.0
