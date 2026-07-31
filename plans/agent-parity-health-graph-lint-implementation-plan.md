# 后端 health、graph、lint 与 Agent 行为对齐计划

## 1. 目标

将 `wiki-backend` 的 maintenance `health`、`graph`、`lint` 服务改为以相邻
`llm-wiki-agent` 的以下脚本为行为基准：

```text
llm-wiki-agent/tools/health.py
llm-wiki-agent/tools/build_graph.py
llm-wiki-agent/tools/lint.py
```

### 实施进展（2026-07-30）

- 已完成：Health、Graph、Lint 的后端私有实现、任务 options、Agent 兼容的 Lint
  语义采样路径、图谱 artifact/report/HTML、质量快照兼容解析，以及共享 parity fixture
  的 Windows 回归测试。fixture 覆盖空页、短页、嵌套页、索引差异、未记录 source、
  两个链接社区与 phantom hub，并通过预核验的路径/边集合 oracle 验证。
- 已完成：Health 报告、Graph JSON/HTML/report/log 与 Lint 报告/日志的关键 Agent
  契约测试；Graph 支持边类型、置信度筛选、搜索、节点抽屉和关联跳转。Lint 的模型
  失败路径保留确定性报告、返回 `partial` 并保存语义报告的 SHA-256 与长度审计元数据。
- 已完成：`delta`、`risk`、`full`、`selected` 已在 API/README 标识为后端扩展模式；
  本计划已从方案文档更新为实施记录。
- 待完成：在明确的 DGX、MySQL、Wiki 副本和模型费用环境中执行端到端验证；该验证
  可能写入实际 Wiki 与 graph 产物，因此不作为本地单元测试的一部分。

目标是**产物、检查规则和报告语义对齐**，而不是从后端启动 Agent CLI：后端
仍通过自身的 service、任务队列、MySQL 审计、`wiki_lock` 和
`app.llm_config` 执行；不动态导入、`subprocess` 调用或修改
`llm-wiki-agent` 源码。

本计划记录已实施的对齐范围、剩余外部验证和后续验收标准。

## 2. 当前基线与差异

当前工作树中已有 maintenance 框架和初版三项服务，且工作树包含未提交改动。
本次实施必须以这些改动为基线，不能覆盖或重置它们。

| 能力 | 当前后端 | Agent 基准 | 后续对齐重点 |
|---|---|---|---|
| health | 已基本复刻空页、索引和 ingest 日志覆盖检查 | `tools/health.py` | 用共享 fixture 做逐项结果与 Markdown 对照；只修复被 fixture 证明的不一致项。 |
| graph 节点 | 仅写 `id`、`label`、`type`、类型色、`path`、`preview` | 节点还必须有完整 `markdown`，并在社区发现后写 `group`、以社区颜色覆盖 `color`、按度数写 `value` | 补齐节点数据和社区视觉语义。 |
| graph 边/产物 | 使用不同的去重策略、`built_at`/`includes_llm_inference` 字段，HTML 仅输出 JSON | Agent 进行无向去重、输出 `built`，生成可交互的 vis.js 图谱页面和完整图谱报告 | 以 Agent 的 JSON/HTML/报告契约替换当前简化产物。 |
| graph 运行记录 | 不写 Agent 风格的 graph/report 条目 | 重建图谱及生成报告均写入 `wiki/log.md` 顶部 | 在同一把 `wiki_lock` 下按 Agent 的 newest-first 写入规则记录。 |
| lint 确定性检查 | 区分导航孤儿和图谱孤儿，支持别名/锚点/路径 WikiLink | Agent 仅以原始 `[[...]]` 解析、使用 `μ + 2σ` hub-stub 阈值、按 community pair 判断 fragile bridge | 保留 Agent 的图谱规则；导航可达性额外识别本地 Markdown 链接，避免 index 中的 source 页被误报。 |
| lint 语义阶段 | JSON/Pydantic 发现、增量候选和比较组 | Agent 直接抽取遍历到的前 20 页、每页前 1500 字符，要求模型返回 Markdown 四段报告 | 首先恢复 Agent 的可见报告与采样逻辑；任务审计保留在后端，但不能改变报告结论。 |

注意：Agent 的页面排除集合并不完全一致：`health.py` 排除
`health-report.md`，而 `lint.py` 原本不排除。后端 Lint 额外排除生成报告，避免
将运行产物误报为知识页质量问题。

## 3. 需先锁定的执行语义

以下是当前后端为安全/成本做的增强，而 Agent CLI 的默认值不同。实施前要在
PR 描述和 API 文档中明确采用的选项，避免后台任务悄然消耗 LLM 配额：

1. `build_graph.py` 默认 `infer=True`；后端 graph API 已同步为默认
   `infer_relations=true`。
2. `lint.py` 默认总会调用主模型；当前后端允许
   `semantic_analysis=false`，并默认采用增量策略。
3. Agent 语义 Lint 的“前 20 页”来自 `rglob()` 的直接结果，不保证排序、最新
   或风险优先；这是待兼容的历史行为，不应被文档解释成全库巡检。

本实施采用以下确定选项，避免后续编码存在默认值歧义：

1. graph 支持 `infer_relations`（默认 `true`）和 `save_report`（默认 `true`）。
   无论是否保存报告，均写 `graph.json`、`graph.html` 和 graph 重建日志；仅当
   `save_report=true` 时写 `graph-report.md` 及对应 report 日志。默认保留报告，
   使现有 quality snapshot 不发生缺失。默认推断会按页面调用后端自有 LLM 配置，
   因此可能产生费用；可显式传 `infer_relations=false` 跳过推断。
2. lint 保留 `semantic_analysis=true`、`semantic_mode=delta` 的后端默认值，避免
   工作流在不知情的情况下改为 Agent 的遍历顺序。新增显式
   `semantic_mode=agent_compat`；该模式与 Agent CLI 使用相同的前 20 页、每页
   1500 字符和 Markdown 报告契约；输出预算使用后端 `.env` 的
   `WIKI_BACKEND_LLM_MAIN_MAX_TOKENS`。
3. 质量工作流固定使用 `graph={infer_relations:false, save_report:true}`，避免批量
   工作流意外产生模型调用；直接 Graph API 则与 Agent CLI 一致，默认开启推断。

这样既能满足逻辑对齐，也不把后台安全策略混入图谱或 Lint 算法。

## 4. 目标行为

### 4.1 Health

以 `tools/health.py` 为唯一规则来源，保留无 LLM 的任务语义：

1. 扫描 `wiki/**/*.md`，排除 `index.md`、`log.md`、`lint-report.md`、
   `health-report.md`。
2. 去 frontmatter 后以 100 字符判断 `empty` / `stub`，保留 `path`、
   `total_bytes`、`body_bytes`、`status`。
3. 从 `index.md` 的 Markdown 链接计算双向索引差异；`overview.md` 和报告页
   不参与索引差异。
4. 只对 `wiki/sources/*.md` 检查 `log.md` 的 `ingest` 记录，按 slug 或
   解转义后的 frontmatter `title` 匹配。
5. `save_report=true` 时生成同标题、同区块、同成功/失败文案的
   `wiki/health-report.md`；`false` 时不写文件。

后端仍可额外更新任务进度和返回不含正文的 `result_summary`，但这些管理字段不
得影响检查结果或报告内容。

### 4.2 Graph

以 `tools/build_graph.py` 的构图和渲染契约替换当前简化实现。

1. 分别按 Agent 的 graph 页面排除规则读取页面；节点包含：
   `id`、`label`、`type`、`color`、`path`、`markdown`、`preview`。
2. 显式边使用 Agent 的原始 Wikilink 提取与 stem 解析，保留
   `EXTRACTED`/`INFERRED`/`AMBIGUOUS` 类型、颜色、置信度、关系标题和 edge id。
3. 推断模式使用后端 `call_llm_fast`，但迁移 Agent 的缓存、checkpoint、断点
   恢复和边过滤/去重语义；缓存格式由后端拥有，不能读取 Agent 的 Python 模块。
4. 按 Agent 的无向边去重规则保留最高置信度边；为缺省字段补齐
   `id`、`color`、`confidence`、`title`、`label`。
5. 用 `networkx` Louvain（seed=42）计算社区。每个节点写入 `group`；有社区的
   节点按 `COMMUNITY_COLORS[group % len(COMMUNITY_COLORS)]` 重新着色，无社区为
   `-1` 并保留类型色。
6. 用所有边计算度数，给每个节点写入 `value = degree + 1`，确保孤儿节点也可见。
7. `graph/graph.json` 使用 Agent 的 `{"nodes", "edges", "built"}` 外部契约。
   仅供后端 API 使用的 metadata 必须放在任务 `result_summary`，不要污染该文件。
8. 移植 Agent 的交互式 `render_html()`，使 `graph/graph.html` 支持边类型/置信度
   筛选、搜索、节点抽屉、完整 Markdown 渲染和关联节点跳转；不再写 `<pre>` JSON。
9. 移植 `generate_report()`、phantom hub、god node、fragile bridge、community
   overview 和建议动作；`save_report` 决定是否写 `graph-report.md` 及 report
   日志。图谱重建始终按 Agent 语义写入 `wiki/log.md`。

`networkx` 缺失或 LLM 推断失败时，继续沿用后端的任务状态约定：保留已完成的
确定性产物，并标记任务为 `partial`；报告中必须明确相应阶段未完成，不能伪装成
Agent 的完整成功。

### 4.3 Lint

后端的确定性检查以 `tools/lint.py` 为图谱规则基准，并修正导航可达性误报；语义报告先恢复 Agent 格式：

1. 排除生成报告；解析 `[[目标|别名]]`、`[[目标#锚点]]` 与路径式 WikiLink。
2. 无任何可解析 WikiLink 或本地 Markdown 入链时记为 `orphan` warning；只缺
   WikiLink、但可由 Markdown 导航时记为 `graph_orphan` info。这样不把 index 中
   的 `sources/*` 页面误判为导航孤儿。
3. 继续计算 broken Wikilink、被至少 3 个页面提及的 missing entity，以及少于 2 个
   不同出站链接的 sparse page；`overview.md` 的 orphan 和 density 规则保持相同。
4. 读取 Agent 契约的 `graph/graph.json`。graph 不存在、损坏或为空时，仅跳过
   graph-aware 检查并输出 Agent 同义提示，不能因旧图谱缓存继续产出结论。
5. hub-stub 使用全图 degree 的 `mean + 2 * stdev` 阈值和页面内容少于 500 字符；
   fragile bridge 按社区对只有一条跨社区边判断；isolated community 忽略单节点
   community，检查没有外部边的多节点社区。
6. `semantic_mode=agent_compat` 时，按 `rglob()` 原顺序取前 20 页，每页注入相对
   路径及前 1500 字符，使用未覆盖 token 预算的 `call_llm_main()`，由
   `WIKI_BACKEND_LLM_MAIN_MAX_TOKENS` 控制，并使用 Agent 的四段 Markdown提示词和
   报告拼装顺序。
7. 生成 `wiki/lint-report.md`：结构问题、图谱观察、图谱问题、分隔线及 Agent 原样语义报告；
   成功写报告后在 `wiki/log.md` 顶部写入 lint 记录。

现有 `maintenance_page_state`、`maintenance_findings` 和 JSON 语义发现不能被静默
删除：它们是后端审计功能。实施时将它们与 `agent_compat` 分离：确定性发现继续
结构化落库；Agent 语义 Markdown 作为报告正文和受限的任务元数据保存。原
`delta` / `risk` / `selected` 的增强语义模式是否保留，需标记为“非兼容模式”，并
不得作为默认兼容路径或与 Agent 对齐测试的依据。

## 5. 实施步骤

### 阶段 A：冻结基准并建立 fixture

1. 先修复当前 Lint 测试把运行日期写死导致的基线失败，随后运行完整测试确认绿灯。
2. 新建仅测试使用的小型 Wiki fixture：含 frontmatter、空页、短页、嵌套页、
   index 差异、未记录 source、双向链接、缺失链接、sparse/overview、多个社区和
   phantom hub。
3. 使用 Agent 脚本的纯函数或预先核验的 expected JSON/Markdown 作为 oracle；
   测试不得调用真实模型、写真实 Agent wiki 或修改 Agent 源码。
4. 将 `graph.save_report` 与 `lint.semantic_mode=agent_compat` 加入 schema、
   默认值、API OpenAPI 和 workflow 回归测试。
5. 把每个脚本的排除集合、路径格式、日期字段和 log 插入位置写成显式断言。

验收：测试能先证明当前后端在 graph 节点字段、JSON 字段、HTML、报告及 lint
graph-aware 规则上与 Agent 存在差异。

### 阶段 B：Health 严格回归

1. 将 `HealthMaintenanceService` 的可比逻辑逐条映射到 `health.py`。
2. 只修复 fixture 发现的不一致；不借机改变任务队列、API 或已有健康路由。
3. 扩展 health 测试为结构化结果和完整报告快照对照。

验收：相同 fixture 的检查结果与 `health.py` 完全相同，`save_report` 的写入行为
与 Agent `--save` 相同，且没有 LLM 或 Quartz 发布副作用。

### 阶段 C：Graph 产物契约迁移

1. 将节点、边、去重、社区、颜色和 `value` 算法移入
   `GraphMaintenanceService`；保持完整类型标注和后端日志。
2. 迁移 graph JSON/HTML/报告渲染及 log 写入，保留 `wiki_lock` 和任务进度。
3. 将可选推断的 checkpoint/cache 行为适配为后端私有实现，并分别测试无推断、
   成功推断、推断失败和 `networkx` 不可用。
4. 更新 `QualityReportService` 与图谱读取代码，使其接受 `built` 字段且不依赖
   已删除的 `built_at` / `includes_llm_inference` 文件字段。

验收：`graph.json` 的节点均具 `markdown`、`group`、`value`；有社区的节点颜色
来自 community palette；`graph.html` 可显示完整 Markdown；Agent/后端 fixture 的
节点、边、社区、报告和 log 语义一致。

### 阶段 D：Lint 兼容路径迁移

1. 将图谱规则和报告模板改为 Agent 语义，并将导航可达性与图谱连接度分离。
2. 新增 `semantic_mode=agent_compat` 的 schema 校验、服务分支、模型调用封装和
   脱敏错误日志；模型失败时遵循后端 `partial` 状态，同时保留确定性报告。
3. 调整结构化 findings 的转换，确保落库不改变报告文字；将旧的增量语义模式显式
   标识为扩展模式或在确认后移除。
4. 为 LLM 调用使用 mock，断言采样页数、顺序、每页 1500 字符截断、`max_tokens`
   和四段 Markdown 输出均符合 Agent。

验收：同一 fixture 下 graph-aware lint 与 Agent 结论一致；Markdown index 中的 source
页不再作为导航 orphan 告警；兼容模式生成的报告与 Agent 结构一致；不调用真实 LLM 时可稳定验证失败和 partial 路径。

### 阶段 E：接口、文档与端到端验证

1. 对照维护 API 的 options，说明 Agent 兼容模式与后端扩展模式、费用和写 Wiki
   副作用；更新 README、`AGENTS.md`（若运行约定变化）及 API 路由文档。
2. 更新 quality snapshot 解析与测试，确认 report/graph metadata 字段变化不会让
   `/api/quality/latest` 读取旧结构。
3. 运行 Windows 完整测试；随后在 DGX ARM64 上执行无 LLM health、无推断 graph、
   无语义 lint，再在受控密钥环境中执行一次 Agent 兼容的图谱和 lint。

## 6. 计划修改的文件

```text
app/services/health_maintenance_service.py
app/services/graph_maintenance_service.py
app/services/lint_maintenance_service.py
app/schemas/maintenance.py
app/services/quality_report_service.py
app/api/maintenance.py                  # 仅当 options/文档需更新
tests/test_health_maintenance_service.py
tests/test_graph_maintenance_service.py
tests/test_lint_maintenance_service.py
tests/test_quality_report_service.py
README.md                               # 仅当 API 默认/执行语义改变
AGENTS.md                               # 仅当运维约定改变
```

不修改 `../llm-wiki-agent` 任何源码；不修改 Quartz、Nginx、FRP、后端监听地址或
发布链路。图谱/报告生成也不自动重建 `quartz/public/`。

## 7. 验证矩阵

| 层级 | 验证 |
|---|---|
| 单元 | Health result/report、graph nodes/edges/group/color/value/HTML/report、lint structure/graph-aware/agent-compatible prompt/report/log。 |
| 任务框架 | 成功、`partial`、失败、依赖失败、进度和 MySQL 审计不回归。 |
| API | options 校验、202 仅代表入队、查询不泄漏 Wiki 全文或绝对路径。 |
| Windows | `.venv\Scripts\python.exe -m unittest discover -s tests -v`。 |
| DGX ARM64 | `.venv/bin/python -m unittest discover -s tests -v`，并在受控环境验证一次任务产物和 `GET /api/quality/latest`。 |

真实 graph 推断和语义 lint 都可能调用模型并写入 `llm-wiki-agent/wiki` 或
`graph/` 产物；端到端验证前必须明确使用的 Wiki 副本、MySQL 环境和模型费用。

## 8. 完成标准

1. 后端不运行或导入 Agent Python 源码，但相同 Wiki fixture 的 health/graph/lint
   规则、报告和数据产物与 Agent 一致。
2. Graph 节点完整写入 `markdown`、`group`、`value`，并按社区重新上色；HTML 不再
   是 JSON 占位页。
3. Lint 的 graph-aware 结论与 Agent 一致；导航可达性不会将 Markdown index 中的
   正常页面误报为 orphan，且存在可审计的 Agent 兼容语义模式。
4. 后端的队列、锁、日志、错误脱敏、`partial` 状态和 API 安全边界继续有效。
5. Windows 和 DGX ARM64 测试通过；任何实际模型/Wiki 写入副作用均已明确验证。
