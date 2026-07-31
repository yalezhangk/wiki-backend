# wiki-backend 知识库运维任务计划

## 1. 目标

在 `wiki-backend` 实施一个受控、可审计、可恢复的运维任务框架，并以该框架实现与 `llm-wiki-agent` 语义兼容的三项能力：

```text
health  → 结构健康检查
graph   → 图谱构建与图谱健康报告
lint    → 确定性质量检查 + LLM 语义巡检
```

本计划的结果不是“把 Agent CLI 包装为 HTTP 命令”，而是：

- Agent 继续是规则与报告格式的权威来源。
- 后端以自身的服务层、`app/llm_config.py`、MySQL、日志和单一 Wiki 锁实现等价工作流。
- Quartz 通过已有/后续的 `GET /api/quality/latest` 读取最新任务产物，而非在访问页面时运行检查。

## 2. 范围与非目标

### 本期范围

1. 通用的 MySQL 运维任务模型、任务队列、执行器、恢复机制和查询 API。
2. `health`、`graph`、`lint` 三种任务。
3. 手动单任务触发和受控的 `health → graph → lint` 质量巡检工作流。
4. 向质量快照读取服务提供任务时间、状态、覆盖范围和报告文件新鲜度。
5. 在成功/部分成功/失败后可追溯的任务记录、脱敏日志和 API 响应。

### 明确不在本期

- `refresh`、`heal`、批量/目录 ingest、`file_to_md`、专用 PDF/arXiv 转换、query 直接保存。
- 直接运行、动态导入或 `subprocess` 调用 `llm-wiki-agent/tools/*.py`。
- 自动写入实体页、自动修复坏链、自动替换矛盾口径。
- 面向公网的无认证写操作。
- 让每次 ingest 自动运行 LLM Lint。
- Quartz UI 的具体实现；该工作由独立 Quartz 线程按 `knowledge-quality-development-plan.md` 执行。

## 3. 架构和执行边界

```text
受保护的管理 API
  → MaintenanceService
  → MySQL maintenance_jobs（排队、依赖、审计、恢复）
  → 单 maintenance-worker
       └─ app.state.wiki_lock
            ├─ HealthMaintenanceService
            ├─ GraphMaintenanceService
            └─ LintMaintenanceService
                 └─ app.llm_config.call_llm_main（仅语义阶段）
                    ↓
             llm-wiki-agent/wiki 与 graph/ 报告产物
                    ↓
             GET /api/quality/latest（只读展示）
```

### 必须遵守的边界

1. 复用 `app.state.wiki_lock`，与 ingest、synthesis、publish 使用同一把锁。所有任务在读取 Wiki 快照、写报告、写图谱及写 `wiki/log.md` 时持锁，避免半写入状态被发布或另一个任务读取。
2. 后端以 `WIKI_AGENT_REPO_PATH` 定位 `wiki/`、`raw/`、`graph/` 数据；它不是 Python import path。
3. 图谱和 lint 的 LLM 调用只使用 `app.llm_config.py`，不依赖 `tools.llm_config.py`。
4. 图谱/质量报告写入后不自动调用 PublishService。质量页通过 API 读取运行时快照，静态页面无需因报告更新重建。
5. 各任务的报告路径只在服务端使用；API 只返回相对展示名，不暴露 Windows 或 DGX 绝对路径。
6. worker 仍以 `wiki-backend` 的进程内单线程运行，MySQL 的领取逻辑负责重启恢复和未来多进程安全；不要另起未受监管的 daemon。

## 4. 任务框架

### 4.1 数据库模型

在 `app/storage/mysql.py::initialize()` 中新增 `maintenance_jobs` 表，使用同一套“启动时建表 + 兼容升级 + 索引”模式，不修改既有 chat、ingest、publish 数据：

```text
id                  BIGINT UNSIGNED PK AUTO_INCREMENT
task_kind           VARCHAR(16)  health | graph | lint
status              VARCHAR(16)  queued | running | succeeded | failed
result_state        VARCHAR(16)  complete | partial | unavailable
trigger_kind        VARCHAR(16)  manual | automatic | workflow
workflow_id         CHAR(36) NULL
depends_on_job_id   BIGINT UNSIGNED NULL
stage               VARCHAR(32)
progress_percent    TINYINT UNSIGNED
request_options     JSON
result_summary      JSON
error               TEXT NULL
created_at          DATETIME
started_at          DATETIME NULL
updated_at          DATETIME
finished_at         DATETIME NULL
```

索引：

- `(status, created_at)`：worker 领取任务。
- `(workflow_id, id)`：查询一次质量巡检的子任务。
- `(task_kind, finished_at)`：读取最近成功任务。
- `depends_on_job_id`：依赖检查。

约束：`depends_on_job_id` 可为空；依赖任务失败时，子任务不执行，子任务标记为 `failed`，错误为“dependency job failed”，保留任务审计。

为使语义巡检能增量、轮换且可解释，新增两个与 `maintenance_jobs` 关联的表；它们只记录页面指纹和质量发现，绝不复制原始全文：

```text
maintenance_page_state
page_path                 VARCHAR(512) PK
content_hash              CHAR(64)
last_structural_checked_at DATETIME NULL
last_semantic_checked_at  DATETIME NULL
last_semantic_content_hash CHAR(64) NULL
last_semantic_job_id      BIGINT UNSIGNED NULL

maintenance_findings
id                        BIGINT UNSIGNED PK AUTO_INCREMENT
job_id                    BIGINT UNSIGNED
finding_type              VARCHAR(32)  contradiction | stale_content | data_gap | concept_depth
severity                  VARCHAR(16)
affected_pages            JSON
evidence                  JSON
recommendation            TEXT
confidence                DECIMAL(4,3) NULL
review_status             VARCHAR(16)  needs_review | confirmed | dismissed
created_at                DATETIME
```

`maintenance_page_state` 以内容哈希、而非文件系统枚举顺序或修改时间，判断页面是否已被当前版本的语义巡检覆盖。为 `last_semantic_checked_at` 和 `job_id` 建立查询索引。`maintenance_findings.evidence` 只保存用于人工核对的短摘录、页面路径与位置，不保存整篇 Wiki 正文。

### 4.2 Pydantic Schema

新增 `app/schemas/maintenance.py`：

```text
MaintenanceTaskKind       health | graph | lint
MaintenanceJobStatus      queued | running | succeeded | failed
MaintenanceResultState    complete | partial | unavailable
MaintenanceTrigger        manual | automatic | workflow

MaintenanceJobCreateRequest
MaintenanceWorkflowCreateRequest
MaintenanceJobResponse
MaintenanceWorkflowResponse
```

请求选项只允许以下白名单：

| 任务 | 允许选项 | 默认值 |
|---|---|---|
| health | `save_report` | `true` |
| graph | `infer_relations` | `false` |
| lint | `semantic_analysis`、`semantic_mode`、`selected_page_paths` | `true`、`delta`、空 |

说明：Agent 的 `build_graph.py` 默认会做 LLM 推断；后端任务默认 `infer_relations=false`，因为质量检查所需的显式 Wikilink 图谱不应隐式消耗 LLM 预算。只有受保护的人工图谱任务可显式打开 LLM 推断。

`MaintenanceJobResponse.result_summary` 按任务类型返回结构化摘要，而不是大段 Markdown 或完整页面正文。

`semantic_mode` 只能为 `delta`、`risk`、`full` 或 `selected`：

- `delta`：默认；优先本页内容哈希自上次成功语义检查后发生变化的页面。
- `risk`：优先确定性 Lint 风险页和图谱关键页。
- `full`：按最久未检查优先的滚动批次推进全库覆盖；一次任务仍受服务端预算约束，不把全库塞进一个 Prompt。
- `selected`：仅检查 `selected_page_paths`，用于人工复核；路径必须归属于 Wiki 且数量不得超过服务端上限。

页面数上限和上下文预算属于服务端配置，不允许 API 调用者提交任意大的 token/字符预算。默认配置可从“最多 20 个候选页面、总输入约 24,000 字符”起步，并以实际模型 tokenizer 和运行数据校准。

### 4.3 领取、执行与恢复

新增 `app/services/maintenance_service.py`：

1. 服务启动时调用 `recover_maintenance_jobs(now)`，把上次进程中仍为 `running` 的任务标记 `failed`，错误为 `maintenance worker restarted`。
2. 单 worker 循环通过 `claim_due_maintenance_job()` 使用事务和 `SELECT ... FOR UPDATE` 原子领取一个依赖已成功的 `queued` 任务。
3. 每个任务更新 `stage` 和 `progress_percent`；范围固定为 `queued=0`、`running=5`、任务内阶段、`completed=100`。
4. 任务异常记录脱敏错误摘要到 MySQL，完整异常只写后端日志；不记录完整 Wiki 正文、LLM Prompt 或密钥。
5. `result_state=partial` 用于“确定性部分已完成，但可选 LLM 语义阶段失败”；这种任务仍为 `succeeded`，但质量 API 和 UI 必须标明语义结论不可用。
6. 不实现删除、重试覆盖或取消 API。第一次上线通过重新创建任务重试，避免中断写入的复杂性。

### 4.4 API

新增 `app/api/maintenance.py`，路由前缀：

```text
POST /api/maintenance/jobs
GET  /api/maintenance/jobs?limit=20&task_kind=&workflow_id=
GET  /api/maintenance/jobs/{job_id}
POST /api/maintenance/workflows/quality
```

行为：

- 单任务创建返回 `202 Accepted`。
- `POST /api/maintenance/workflows/quality` 创建同一 `workflow_id` 下的三项依赖任务：`health` → `graph` → `lint`，返回所有子任务 ID。
- `GET` 仅返回任务审计和结构化摘要，不返回原始 Markdown 报告或 LLM 原始回答。
- 路由文档明确说明“入队不代表检查完成”，并且 Lint 可能产生 LLM 费用。

### 4.5 安全与部署前提

当前 API 没有完整公网身份体系。因此：

1. `POST /api/maintenance/*` 在 DGX/ECS Nginx 层必须先配置 HTTPS、认证和限流后才能对公网开放。
2. 在此之前，仅允许 DGX 回环/受控管理员网络调用；生产反向代理对外拒绝该前缀的 POST。
3. `GET /api/maintenance/*` 同样视为管理信息，默认不接入 Quartz 普通页面。
4. `/api/quality/latest` 继续是面向质量页的只读、脱敏快照接口；它不提供任务执行权限。

## 5. Health 任务

新增 `app/services/health_maintenance_service.py`，保持与 `tools/health.py` 一致的业务语义：

1. 扫描 `wiki/**/*.md`，排除 index、log、health/lint report 等元文件。
2. 去除 frontmatter 后，以 100 字符阈值检测空页/短页。
3. 比较 `wiki/index.md` 链接与磁盘页面，输出“索引存在但文件缺失”及“文件存在但未索引”。
4. 对 `wiki/sources/*.md` 检查 `wiki/log.md` 是否有对应 ingest 记录；支持 slug 和 frontmatter title 的轻量化匹配。
5. 生成结构化 `HealthResult`，并在 `save_report=true` 时写入 `wiki/health-report.md`。

任务阶段：

```text
scanning_pages (20)
checking_index (45)
checking_log (70)
writing_report (90)
completed (100)
```

成功摘要至少包含：扫描页数、empty/stub 数、索引双向差异数量、日志缺失数量、报告时间。

Health 是无 LLM、可重复运行的任务。后续可由 ingest/synthesis 成功后自动入队，但本期先完成手动任务和 workflow 中的任务；自动触发作为最后一项可选增强，避免在首次上线时隐藏副作用。

## 6. Graph 任务

新增 `app/services/graph_maintenance_service.py`，将 Agent 图谱逻辑迁移为后端服务，不调用 `tools/build_graph.py`：

### 6.1 确定性图谱构建

1. 扫描与 Health 相同范围的 Wiki 页面。
2. 解析 frontmatter type、title、页面预览和显式 `[[wikilinks]]`。
3. 建立 `EXTRACTED` 边、去重、保留 `id/from/to/type/confidence`。
4. 使用项目依赖的 `networkx` 执行 Louvain 社区发现；若依赖不可用，任务可完成但 `result_state=partial`，报告明确“社区检查跳过”。
5. 生成 `graph/graph.json`、`graph/graph-report.md` 与与现有图谱页面契约兼容的 `graph/graph.html`。

### 6.2 可选语义边

当 `infer_relations=true` 时：

1. 使用 `app.llm_config.call_llm_fast`，不使用 Agent 源码配置。
2. 每页只提供被截断的必要上下文，严格 Pydantic/JSON 校验 LLM 返回的目标页、关系、置信度和类型。
3. 仅允许目标指向当前已存在页面；`confidence >= 0.7` 为 `INFERRED`，其余为 `AMBIGUOUS`。
4. 支持缓存/断点恢复，但缓存文件格式与路径必须由后端服务拥有并写明兼容策略；LLM 失败时保留已完成的确定性图谱，任务为 `succeeded + partial`。

### 6.3 图谱报告和任务摘要

报告与摘要至少包括：节点数、边数、显式/推断边数量、边密度、孤儿节点、hub/god node、脆弱桥接、孤立社群、phantom hub、`built_at` 与图谱是否包含 LLM 推断。

任务阶段：

```text
reading_pages (15)
building_nodes (30)
extracting_links (45)
inferring_relations (可选，50-75)
detecting_communities (82)
writing_graph (92)
completed (100)
```

图谱完成后，质量快照读取服务必须使 graph 缓存失效或依据 mtime 自动失效，确保 `/api/quality/latest` 立即看到新的图谱时间与结果。

## 7. Lint 任务

新增 `app/services/lint_maintenance_service.py`。Lint 分两个明确阶段，避免把 LLM 结果伪装成确定性结论。

### 7.1 确定性阶段

复用后端内部的页面、链接和图谱读取工具，检查：

- orphan pages（无入站链接）。
- broken wikilinks（目标不存在）。
- missing entity candidates（同一缺失 WikiLink 至少出现在 3 页）。
- sparse pages（少于 2 个不同出站 WikiLink）。
- graph-aware：hub stub、fragile bridge、isolated community。

若 graph 缺失/过期，确定性文本链接检查仍执行，但 graph-aware 部分标记为 `skipped`，绝不回填旧图谱数据。

### 7.2 语义阶段

当 `semantic_analysis=true` 时：

1. 不复刻 Agent 的 `pages[:20]` 逻辑。该逻辑只是为避免一次发送过多上下文而直接取 `rglob()` 遍历结果的前 20 页；它既不是“最新 20 页”，也不是风险最高的 20 页。
2. 先对全部页面完成确定性阶段并写入本次页面内容哈希，再按 `semantic_mode` 生成候选页。默认 `delta` 的优先级固定为：内容哈希变化或新页面 → 本次确定性风险页 → 图谱桥接/高中心度/跨社区相关页 → 最久未被当前内容版本语义检查的页面。相同优先级按标准化相对路径排序，保证任务可重现。
3. 不把页面孤立地截取后混装。候选页按显式链接、共同缺失实体和图谱社区组成 2～6 页的“比较组”；新变更页必须尽量带上其强关联页面，才有发现不同口径结论的机会。无法找到关联页时，允许单页检查，但该组不能产出“跨页矛盾”结论。
4. 在服务端配置的总输入预算内顺序装入比较组。默认最多覆盖 20 个候选页、总输入约 24,000 字符；达到页面数或总预算即停止。每页传入标题、路径、frontmatter 摘要、相关段落短摘录及可定位的证据位置，不能简单固定取全文前 1500 字符。实际 token 使用量、被跳过的候选页和选择原因必须记录到任务结果。
5. 使用 `app.llm_config.call_llm_main`。Prompt 要求输出结构化 JSON：`contradictions`、`stale_content`、`data_gaps`、`concepts_needing_depth`。每项必须包含涉及页面、双方/相关结论、短证据、建议核对来源、`confidence` 和 `needs_review` 状态；证据不足时必须返回空数组，不能猜测。
6. 对 LLM JSON 使用 Pydantic 校验，合格发现写入 `maintenance_findings`。解析失败仅使语义阶段失败，确定性结果和 Lint 报告仍写入。
7. 报告和 API 返回本次覆盖（候选页数、实际检查页数、比较组、选择原因、模型、token/字符使用量、状态）以及累计覆盖（当前内容哈希已语义检查的页面数 / 总页面数）。UI 不得将本次或累计覆盖称为“全库无矛盾证明”。

当 `semantic_analysis=false` 时，不调用 LLM，任务可成功，报告显示“语义阶段未运行”。

### 7.3 报告与日志

写入 `wiki/lint-report.md`，报告顺序固定：

```text
结构问题
图谱问题
语义问题（含涉及页面、冲突口径、证据、建议核对来源、置信度与待确认状态）
数据缺口与建议来源
执行范围、候选依据与覆盖限制
```

同时按当前 Wiki 约定向 `wiki/log.md` 顶部写入新的 `lint` 记录。写入必须在 `wiki_lock` 内完成，且只在报告成功生成后写日志。

任务阶段：

```text
loading_wiki (10)
checking_links (30)
checking_graph (48)
semantic_analysis (可选，55-80)
writing_report (92)
completed (100)
```

## 8. 质量巡检工作流

`POST /api/maintenance/workflows/quality` 创建如下依赖链：

```text
health (workflow)
  ↓ 仅成功后继续
graph (workflow, infer_relations=false)
  ↓ 仅成功/partial 后继续
lint (workflow, semantic_analysis=true)
```

规则：

- Health 失败：Graph 和 Lint 不执行，分别标记依赖失败。
- Graph `partial`：Lint 可以继续，但必须把 graph-aware 质量结论标为部分可用。
- Lint 语义阶段失败：Lint 任务成功且 `result_state=partial`，质量页展示结构/图谱结果与“语义巡检不可用”。
- 工作流结束后不自动发布 Quartz；`/api/quality/latest` 读取新报告即可更新运行时 UI。

## 9. 与现有功能的集成

### 9.1 Ingest 与 Synthesis

第一期不改变现有 ingest/synthesis 成功后的 PublishService 调度。

第二小步（仅在任务框架稳定后）可增加：

```text
ingest / synthesis 成功 → 自动排一个 health 任务
```

不自动排 lint；图谱只允许通过人工质量工作流或明确的批处理策略启动。这样不会让频繁上传资料导致 LLM 成本、长时间锁或重复图谱构建。

### 9.2 Quality Snapshot API

质量快照读取服务应优先读取本计划生成的健康、图谱、Lint 报告和最近成功 maintenance job：

- 有任务但报告不存在：返回 `incomplete`。
- 最近任务失败：返回 `failed` 的说明，但保留上一份成功报告的时间作为历史信息，不能当当前结果。
- Lint `partial`：结构数据可展示，语义状态为不可用。
- Lint 语义阶段成功：返回本次语义覆盖和累计语义覆盖，例如“本次 18 / 148 页；当前内容版本累计 102 / 148 页”，以及候选选择依据；前端分别展示结构覆盖与语义覆盖。

### 9.3 Publish 与 Quartz

- maintenance 任务不直接构建 `quartz/public`。
- Quartz `/quality` 使用同源 API 读取最新结果，不需要等待静态重建。
- 若图谱页面的静态入口依赖 `graph.html`，由后续 Quartz 线程单独确认其发布链；不得在本后端计划中隐式耦合 Nginx 或公开端口。

## 10. 实施顺序

### 里程碑 1：框架与数据库

1. 增加 schemas、`maintenance_jobs` DDL、storage 方法、fake storage。
2. 实现 worker 领取、状态更新、依赖、恢复和 API 路由。
3. 先用无副作用 fake handler 验证队列、依赖和重启恢复。

验收：任务创建返回 `202`；同一 workflow 的依赖正确；运行中重启后任务有可追溯失败状态；无 MySQL 时返回 `503`。

### 里程碑 2：Health

1. 移植 Health 确定性规则与 Markdown 报告格式。
2. 添加 health fixture 对比测试。
3. 接入真实任务 worker 与 `GET /api/maintenance/jobs/{id}`。

验收：对固定 Wiki fixture 的空页、索引、日志结果与 Agent 规则一致；任务只写 `health-report.md`，不调用 LLM、PublishService 或 Quartz。

### 里程碑 3：Graph（先确定性，后可选推断）

1. 实现节点、显式边、社区、报告和 JSON/HTML 输出。
2. 实现 `infer_relations=false` 的完整成功路径。
3. 添加可选 LLM 推断和 partial 失败路径。

验收：图谱任务能在没有 LLM 的情况下构建可用 `graph.json` 与报告；LLM 推断失败不破坏确定性图谱。

### 里程碑 4：Lint

1. 实现确定性链接/图谱检查。
2. 实现 `maintenance_page_state`、候选排序、关联比较组和服务端输入预算。
3. 实现语义 LLM JSON 契约、`maintenance_findings` 持久化与部分成功。
4. 写入 Lint 报告和 log 记录，接入质量快照读取。

验收：内容发生变化的页面优先进入语义巡检；同一 Wiki 快照的候选顺序可复现；可产生带证据和待确认状态的结构化矛盾候选；明确展示本次/累计覆盖；失败时不伪称“无矛盾”。

### 里程碑 5：工作流、安全与 DGX

1. 实现 `health → graph → lint` workflow。
2. 配置 DGX/ECS 管理 API 保护策略后，才允许实际管理 POST。
3. 在 DGX ARM64 上运行完整测试与一次受控真实工作流。

## 11. 测试矩阵

### 单元测试

- `tests/test_maintenance_service.py`：排队、领取、依赖、partial、恢复、错误脱敏。
- `tests/test_maintenance_api.py`：202、列表/详情、参数校验、503、无 raw report 泄露。
- `tests/test_health_maintenance_service.py`：空页、索引双向差异、日志覆盖。
- `tests/test_graph_maintenance_service.py`：显式链接、孤儿、社区、缺失 networkx、推断 LLM 失败。
- `tests/test_lint_maintenance_service.py`：orphan、broken、missing entity、sparse、过期图谱、语义 JSON 成功/失败/未运行；内容哈希增量选择、风险优先、最久未查轮换、关联比较组、服务端预算截断和单页不得报跨页矛盾。
- 更新质量快照 API 测试：最新 maintenance job 与报告状态映射。

### Windows 验证

```powershell
cd C:\job_docs\knowledge_base\mvc_sample\wiki-backend
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

### DGX 验证

```bash
cd /home/dgx/Projects/knowledge_base_mkt/wiki-backend
.venv/bin/python -m unittest discover -s tests -v
curl --fail --silent --show-error http://127.0.0.1:8081/api/health
```

随后通过受保护的管理入口创建一次 quality workflow，逐项确认 health、graph、lint job 的状态、报告时间、`/api/quality/latest` 和 Quartz `/quality` 的展示一致。

## 12. 完成标准

本计划完成的最低标准：

1. `health`、`graph`、`lint` 都是可查询、可恢复、可审计的后端任务，不是网页同步执行或 Agent 子进程包装。
2. 每项任务都有清晰的输入选项、进度、结果摘要、错误和报告时间。
3. `health → graph → lint` 工作流能正确处理成功、部分成功和依赖失败。
4. Lint 的 LLM 成本、候选依据、本次覆盖和累计覆盖透明可见；语义失败不会覆盖确定性结果，也不伪称“全库无矛盾”。
5. 未认证公网无法触发写 Wiki/LLM 的 maintenance POST。
6. Agent 工具源码保持不变；后端兼容行为由 fixture 测试保障。
7. 质量页可以只读地展示最近一次任务产物，而不需要页面端执行维护操作。
