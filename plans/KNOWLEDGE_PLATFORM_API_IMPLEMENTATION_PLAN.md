# 中压-市场部 样本知识库：wiki-backend API 实施交接

> 状态（2026-08-10）：本文是 Phase B 开发时的历史交接。B0 数字 ID 契约、B1 Ingest
> 阶段/进度、B2 结构化引用以及当时 Deferred 的 Publish 编排均已实现；质量快照当前使用
> `GET /api/quality/latest`。现行接口以 `README.md`、FastAPI OpenAPI、`app/` 与测试为准。

> 用途：在 `wiki-backend` 项目中新开开发线程时，作为背景、范围、接口优先级和验收基线。  
> 产品规格：`../UI_PRODUCT_SPEC.md`  
> 前端交接：`../quartz/KNOWLEDGE_PLATFORM_UI_IMPLEMENTATION_PLAN.md`  
> 更新时间：2026-07-22

## 1. 新线程目标

为“中压-市场部 样本知识库”前端提供真实、稳定、可追溯的业务数据，重点补足：

1. Ingest 任务的真实阶段与进度。
2. Query/Chat 回答的结构化引用。
3. 可选的知识概览与健康摘要。

本任务不负责 Quartz 页面、CSS、静态资源或 Nginx 页面路由，也不在首期自动执行 Quartz build。

## 2. 开发前必须阅读

按顺序阅读：

1. `AGENTS.md`
2. `../AGENTS.md`
3. `../UI_PRODUCT_SPEC.md`
4. `KNOWLEDGE_PLATFORM_API_IMPLEMENTATION_PLAN.md`
5. `app/main.py`
6. `app/api/chats.py`
7. `app/api/ingest.py`
8. `app/api/synthesis.py`
9. `app/schemas/`
10. `app/services/`
11. `app/storage/mysql.py`
12. 对应 `tests/`

计划若与当前代码冲突，以代码和测试事实为准，并在修改前更新本文的假设。

## 3. 当前代码事实

### 3.1 已有接口

| 能力 | 接口 | 当前状态 |
|---|---|---|
| 服务存活 | `GET /api/health` | 只返回 `{"status":"ok"}`，不证明 MySQL、模型或 Wiki 正常 |
| 无状态问答 | `POST /api/query` | 返回 `answer`、`sources`、`relevant_pages` |
| 会话列表/创建 | `GET/POST /api/chats` | MySQL 持久化 |
| 会话重命名 | `PATCH /api/chats/{id}` | 已有 |
| 会话历史/发送 | `GET/POST /api/chats/{id}/messages` | 返回消息及来源字段 |
| 文档入库 | `POST /api/ingest/jobs` | `202 Accepted`，后台线程异步执行 |
| 入库任务列表/详情 | `GET /api/ingest/jobs*` | 已有 |
| 保存分析 | `POST /api/synthesis` | 根据消息 ID 读取已保存回答并写入 Wiki |

### 3.2 已有 Ingest 响应

```text
job_id
status: queued | running | succeeded | failed
original_filename
source_path
created_pages
updated_pages
contradictions
validation.broken_links
validation.unindexed
error
created_at / started_at / finished_at
```

当前没有 `stage`、`progress_percent` 和 `updated_at`。前端在这些字段落地前不得伪造细粒度阶段。

### 3.3 已有引用信息

`QueryResponse` 和助手消息已有：

```text
sources: string[]
relevant_pages: string[]
```

它们足以支撑第一阶段页面链接，但不足以直接呈现标题、对象类型、命中片段和相关度。

## 4. 必须保持的边界

- FastAPI 生产监听 `127.0.0.1:8081`。
- 浏览器只通过 DGX Nginx 的同源 `/api` 访问。
- 不增加 ECS `18081` 或第二条 FRP 隧道。
- MySQL 保存 chat、message、ingest job 等业务状态，不保存 Wiki 正文。
- Wiki 正文继续位于相邻 `llm-wiki-agent/wiki`。
- 不动态导入或执行 `llm-wiki-agent` Python 源码。
- Ingest/Synthesis 的预期业务写入不等于可以修改 agent 源码。
- `POST /api/query` 保持无状态，不隐式创建 Chat。
- `POST /api/synthesis` 继续接受消息身份和可选标题，不接受前端提交的回答正文。
- Ingest 成功不等于 Quartz 已发布。
- 不在本计划首期增加自动 Quartz build、Nginx reload 或缓存清理。

## 5. API 演进原则

- 优先向后兼容新增字段，不直接删除或重命名现有字段。
- 所有边界数据使用 Pydantic。
- 路由、服务、存储职责保持分离。
- 数据库变化必须包含初始化升级、已有数据默认值、索引考虑和测试。
- 错误响应说明可行动原因，不泄露敏感文档、路径、密钥或完整模型请求。
- 新接口必须补充 route `summary`、`description`、函数 docstring 和测试。

## 6. 分阶段计划

### Phase B0：契约基线与前端解阻

目标：确认前端可在不等待后端重构的情况下开始开发。

任务：

- 为现有 Query、Chats、Ingest、Synthesis 响应补齐或核对契约测试。
- 确认所有时间字段的时区/序列化形式。
- 确认 `sources`、`relevant_pages`、`created_pages`、`updated_pages` 都使用 Wiki 相对路径或可稳定映射的标识。
- 更新 FastAPI 应用 description，使其准确包含 chats、ingest 和 synthesis。
- 不改变现有行为，只消除文档与代码不一致。

验收：完整单元测试通过，现有 Quartz Chats 不需要修改即可继续工作。

### Phase B1：Ingest 真实阶段与进度

目标：让入库中心展示真实工作流，而不是猜测百分比。

建议新增向后兼容字段：

```python
IngestStage = Literal[
    "uploaded",
    "converting",
    "extracting",
    "writing_wiki",
    "validating",
    "completed",
]

stage: IngestStage
progress_percent: int
updated_at: datetime
```

实施要求：

- 先根据 `IngestService` 的真实执行边界确定阶段，不为视觉原型虚构不存在的步骤。
- `queued` 可以使用 `uploaded`；`succeeded` 必须使用 `completed`；`failed` 保留失败时最后阶段。
- `progress_percent` 是阶段进度，不表示 Quartz publish 进度。
- 在每个可观察阶段更新 MySQL，而不是只保存在进程内。
- 为已有行提供安全默认值或升级逻辑。
- 高频进度更新不得造成不必要的数据库写放大。
- 保持当前 `status` 字段，前端可渐进使用新字段。

需要修改的典型位置：

- `app/schemas/ingest.py`
- `app/services/ingest_service.py`
- `app/storage/mysql.py`
- `app/api/ingest.py` 的文档
- `tests/test_ingest_service.py`
- `tests/test_ingest_api.py`
- 必要时 `tests/test_mysql_integration.py`

### Phase B2：结构化引用

目标：让知识问答右侧证据栏能够稳定展示页面、类型和片段。

建议先定义统一模型：

```python
class CitationResponse(BaseModel):
    path: str
    title: str
    kind: Literal["source", "entity", "concept", "synthesis", "page"]
    excerpt: str | None = None
    relevance: float | None = None
```

在 `QueryResponse` 和 `ChatMessageResponse` 中新增：

```python
citations: list[CitationResponse] = Field(default_factory=list)
```

兼容要求：

- 保留 `sources` 和 `relevant_pages`。
- 无法可靠生成 `excerpt` 或 `relevance` 时返回 `None`，不得伪造。
- `path` 使用 Wiki 相对路径或可被 Quartz 稳定解析的 slug。
- `title` 和 `kind` 优先来自 frontmatter/真实目录语义。
- 防止 `..`、绝对路径和越界读取。
- 如果 citations 需要在刷新后完全恢复，应明确存储为结构化 JSON；不要只在第一次响应临时拼装。

实现前必须先检查 QueryService 当前检索结果是否包含命中片段和分数；如果没有，分两步交付：

1. 先提供 `path/title/kind`。
2. 后续检索层真实提供片段/分数后再开放 `excerpt/relevance`。

测试至少覆盖：中文路径、缺少 frontmatter、未知类型、空引用、非法路径和历史消息恢复。

### Phase B3：知识概览与健康摘要（按需）

此阶段不是 Quartz 首页和知识库首期的阻塞项，因为前端可以使用 `contentIndex.json`。

只有确认需要运行时数据后再实现：

```text
GET /api/knowledge/summary
GET /api/knowledge/health
```

`summary` 可返回：

```text
source_count
entity_count
concept_count
synthesis_count
latest_updated_at
```

`health` 可返回：

```text
broken_link_count
unindexed_count
contradiction_count
last_checked_at
status
```

约束：

- 只读 `llm-wiki-agent/wiki` 或读取已生成的报告文件。
- 不动态导入 agent 的 health/lint Python 工具。
- 不在每个请求中无缓存地全量扫描大型 Wiki；先定义缓存和失效策略。
- “健康度百分比”必须有稳定计算公式；没有公式时返回计数和状态，不输出伪精确的 `98%`。

### Deferred：Publish 编排

本计划不实现 `POST /api/publish` 或 `/api/publish/jobs`。

未来单独设计时必须先解决：

- 同时只允许一个 Quartz build。
- 连续 Ingest 合并发布。
- 构建到临时目录并原子切换。
- 失败时继续提供上一版 `public/`。
- 子进程权限、超时、日志脱敏和取消。
- DGX Nginx 读取权限。
- ECS 短缓存失效与版本可见性。
- 审计、认证和限流。

在这些问题解决前，后端不得返回虚假的 `published` 状态。

## 7. 建议响应示例

### 7.1 增强后的 Ingest 响应

```json
{
  "job_id": "example-job-id",
  "status": "running",
  "stage": "validating",
  "progress_percent": 82,
  "original_filename": "RM6-SeT-2025.pdf",
  "source_path": "raw/uploads/RM6-SeT-2025.pdf",
  "created_pages": ["sources/rm6-set-2025.md"],
  "updated_pages": ["index.md"],
  "contradictions": [],
  "validation": {"broken_links": [], "unindexed": []},
  "error": null,
  "created_at": "2026-07-22T10:01:08",
  "started_at": "2026-07-22T10:01:10",
  "updated_at": "2026-07-22T10:04:21",
  "finished_at": null
}
```

示例只规定结构，不代表允许把固定 `82` 写入实现。

### 7.2 增强后的回答引用

```json
{
  "path": "entities/smart-hvx.md",
  "title": "Smart HVX",
  "kind": "entity",
  "excerpt": null,
  "relevance": null
}
```

## 8. 安全与副作用

- 上传文件名、大小、类型和落盘路径继续由服务端验证。
- 不在日志中记录完整敏感文档或模型 Prompt。
- 对 Wiki 的读取必须防止路径穿越。
- 真实 Ingest 测试会写 `raw/uploads` 和 `wiki/`，执行前必须明确隔离目录。
- 真实 Synthesis 测试会写 Wiki，默认单元测试使用临时目录和 fake storage。
- MySQL 集成测试只在显式启用的测试库执行。
- 当前 API 未具备完整公网认证；计划不得把安全问题包装成前端隐藏按钮。

## 9. 测试计划

### B0

- Query/Chat/Ingest/Synthesis 现有成功与错误契约。
- 旧客户端不发送/读取新增字段时仍正常。

### B1

- 阶段按真实执行顺序推进。
- 每个阶段的持久化和恢复。
- 失败保留最后阶段与错误。
- 旧数据库行的默认迁移。
- 列表与详情响应一致。

### B2

- citations 的 schema、序列化和持久化。
- sources/relevant_pages 向后兼容。
- 中文路径和标题。
- 路径越界拒绝。
- 空引用和缺失元数据回退。

### B3

- Wiki 为空、目录不存在、文件损坏和缓存失效。
- 统计口径与目录/frontmatter 规则一致。
- 不泄露宿主机绝对路径。

## 10. 验证命令

Windows：

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

涉及启动依赖或导入时，可补充：

```powershell
.venv\Scripts\python.exe -m compileall -q app tests
```

DGX：

```bash
cd /home/dgx/Projects/knowledge_base_mkt/wiki-backend
uname -m
.venv/bin/python --version
.venv/bin/python -m unittest discover -s tests -v

.venv/bin/python -m uvicorn app.main:app \
  --host 127.0.0.1 --port 8081
```

另一个终端验证：

```bash
curl --fail --silent --show-error http://127.0.0.1:8081/api/health
curl --fail --silent --show-error http://127.0.0.1:8081/api/chats > /dev/null
curl --fail --silent --show-error 'http://127.0.0.1:8081/api/ingest/jobs?limit=20' > /dev/null
curl --fail --silent --show-error http://127.0.0.1:8080/api/health
```

真实 MySQL 集成测试：

```bash
WIKI_BACKEND_RUN_MYSQL_INTEGRATION=1 \
  .venv/bin/python -m unittest tests.test_mysql_integration -v
```

## 11. 完成标准

- 新字段与接口都使用 Pydantic 并在 OpenAPI 中有说明。
- 现有 Quartz 客户端保持兼容。
- Ingest 阶段和进度来自真实业务状态。
- 引用数据可追溯，不伪造片段和分数。
- 单元测试通过，必要的 MySQL 升级和集成测试完成。
- DGX 仍监听 `127.0.0.1:8081`，同源 `/api` 正常。
- 未自动构建 Quartz、未增加第二隧道、未修改 agent 源码。
- README、`.env.example` 和接口文档按实际变更同步。

## 12. 建议给新线程的首条指令

```text
请通读 AGENTS.md、../AGENTS.md、KNOWLEDGE_PLATFORM_API_IMPLEMENTATION_PLAN.md
和 ../UI_PRODUCT_SPEC.md，并以当前代码与测试为最终事实。

先检查工作树，然后严格执行 Phase B0：
1. 核对现有 Query、Chats、Ingest、Synthesis 接口契约和测试；
2. 修正文档与代码事实不一致，但不改变业务行为；
3. 明确 Wiki 路径标识、时间字段和向后兼容基线；
4. 使用项目 .venv 运行完整单元测试。

B0 完成后，再评估并实施 B1 的 Ingest stage/progress；
不要提前实现 Publish API，不要修改 llm-wiki-agent 源码，不要暴露 8081。
每个 Phase 完成后报告代码变更、数据库影响、测试结果和下一阶段风险。
```
