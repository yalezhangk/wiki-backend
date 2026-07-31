# 知识质量开发计划（以已确认原型为准）

## 0. 本计划的唯一前端基线

本计划以 [design/ui-prototypes/quality.html](../design/ui-prototypes/quality.html) 为 `/quality` 的**默认桌面视图、信息层级和交互验收标准**。它不是灵感参考，也不是可选方案。

后续前端开发完成后，至少应具备原型中的以下可见结构和行为：

```text
页头：质量巡检快照 + “查看巡检报告” + 受控的“运行新一轮检查”说明
四格快照：报告生成时间 / 检查覆盖 / 图谱状态 / 语义巡检
五个筛选标签：全部 / 内容一致性 / 结构完整性 / 图谱质量 / 新鲜度与修复
两栏主区域：左侧发现项与表格；右侧选中问题的证据对比与检查边界
```

原型内的日期、147 个对象、13 个发现项及示例文本只用于展示；上线数据必须来自后端质量快照，缺失或过期时必须如实展示，不得保留示例数字。

## 1. 目标、范围与硬边界

### 目标

将 Quartz `/quality` 从构建期 frontmatter 缺口页升级为“最近一次已确认质量巡检”的控制台。它需要同时呈现：

1. Agent 报告快照的生成时间、覆盖对象、健康状态和图谱新鲜度。
2. 结构完整性、内容一致性、图谱质量、新鲜度与修复建议。
3. `lint.py` 中的矛盾项的页面、证据、建议和人工确认状态。
4. Quartz 自己可以确认的摘要/标签/更新时间缺口，但明确标注其“静态索引”来源。

### 改动范围

- `wiki-backend`：新增只读质量快照服务与 `/api/quality/latest`。
- `quartz/.local-plugins/knowledge-ui`：重写 `QualityPage`，增加客户端快照加载、标签筛选、证据面板和明确的降级状态。
- `plans/knowledge-quality-development-plan.md`：本计划。

### 不做的事情

- 不修改 `llm-wiki-agent` 代码、提示词或质量工具。
- 不让浏览器、Quartz 或 `wiki-backend` 直接执行 `health.py`、`lint.py`、`refresh.py`、`heal.py`。
- 不让“运行新一轮检查”“修复建议”“标记人工确认”在本期写 Wiki、调用 LLM 或创建后台任务。
- 不新增后端直连端口、第二条 FRP 隧道或跨域访问；生产请求始终通过同源 `/api`。
- 不因读取质量报告触发 PublishService、Quartz build、Nginx reload 或 ECS 缓存清理。

## 2. 运行架构与数据权威性

```text
llm-wiki-agent（质量规则、报告产物的权威来源）
  ├─ wiki/health-report.md
  ├─ wiki/lint-report.md
  ├─ graph/graph-report.md
  └─ graph/graph.json
         ↓ 只读、容错解析
wiki-backend  GET /api/quality/latest
         ↓ 同源 /api，禁用代理缓存
Quartz /quality
  ├─ 服务端静态骨架：导航、五区块、metadata gap fallback
  └─ 浏览器端动态填充：快照、发现项、证据面板、状态与筛选
```

### 数据边界

| UI 区块 | 事实来源 | 没有新鲜报告时的行为 |
|---|---|---|
| 四格巡检快照 | `QualitySnapshotResponse.snapshot` | 显示“最近质量快照不可用”，不显示示例数值 |
| 内容一致性 | `wiki/lint-report.md` 的结构化解析结果 | 显示“尚无可用语义巡检报告” |
| 结构完整性 | health/lint 报告 + Quartz `props.allFiles` metadata gap | Agent 结果不可用；静态 metadata gap 仍可显示为独立来源 |
| 图谱质量 | `graph-report.md` + `graph.json` | 显示“图谱报告缺失/过期”，不把旧数字当当前结论 |
| 新鲜度与修复 | 已存在的新鲜度/缺失实体报告信息 | 显示“尚无来源新鲜度快照”；不自行重跑 refresh/heal |

`lint.py` 的语义检查当前只抽样 `pages[:20]`。因此后端必须在 `semantic_scope` 中返回 `sampled | full | unknown`，前端固定显示其检查范围，不能用“知识库无矛盾”一类绝对表述。

## 3. 后端线程计划：`wiki-backend`

后端线程只负责提供稳定的、只读的界面数据契约。完成后，Quartz 可以独立用 fixture 开发，不依赖本地真实 LLM 或 MySQL。

### 3.1 API 契约（先实现并固定）

新增：

```text
GET /api/quality/latest
```

成功响应始终为 `200`；报告缺失、解析失败、内容过期属于领域状态，不是 HTTP 500。仅 Wiki 根目录不可访问或质量服务未初始化时返回 `503`。

响应模型建议在 `app/schemas/quality.py` 中定义，字段与原型一一对应：

```json
{
  "snapshot": {
    "status": "available",
    "generated_at": "2026-07-29T10:42:00",
    "current_object_count": 147,
    "coverage": { "checked_object_count": 142, "scope": "sampled" },
    "checks": {
      "health": { "state": "available", "generated_at": "...", "message": "结构检查完成" },
      "lint": { "state": "stale", "generated_at": "...", "message": "语义检查报告早于当前 Wiki" },
      "graph": { "state": "available", "generated_at": "...", "message": "图谱与 Wiki 同步" },
      "freshness": { "state": "not_run", "generated_at": null, "message": "尚无来源新鲜度快照" }
    }
  },
  "tab_counts": {
    "all": 13,
    "consistency": 6,
    "structure": 2,
    "graph": 3,
    "freshness": 2
  },
  "structural": { "checks": [] },
  "consistency": { "findings": [] },
  "graph": { "findings": [] },
  "freshness": { "recommendations": [] }
}
```

需要为所有可空/无法判定项使用明确枚举：

```text
available | stale | missing | parse_failed | not_run | incomplete
```

不得以 `0`、空数组或 `null` 隐式表达“报告没跑过”。

### 3.2 Schema 细节

`QualityFinding` 必须支持原型右侧“证据对比”面板：

```text
id                 稳定展示 ID（报告类别 + 标题 + 段落定位派生）
category           consistency | structure | graph | freshness
severity           critical | warning | info | unknown
status             needs_review | documented_difference | unavailable
title              问题标题
summary            左侧列表中的简短冲突/风险摘要
pages[]            涉及的相对 Wiki slug；无法可靠提取时为空数组
evidence[]         最多两个 {label, source_label, location, quote}
recommendation     建议核对来源或人工后续动作
report_section     原报告章节定位，例如 Contradictions
```

`QualityStructuralCheck` 提供 `label`、`state`、`count`、`detail`；Quartz 的 metadata gap 不进入该 API，它由 Quartz 本身在前端显示为“静态索引补充”。

### 3.3 服务实现

新增 `app/services/quality_report_service.py`：

1. 只读固定文件：`wiki/health-report.md`、`wiki/lint-report.md`、`graph/graph-report.md`、`graph/graph.json`。
2. 读取当前可发布 Markdown 页数和最大修改时间，用于判断报告/图谱是否过期。
3. 解析 health 的空页、索引同步、日志覆盖；解析 lint 的 `Structural Issues`、`Contradictions`、`Stale Content`、`Data Gaps`、`Graph-Aware Issues`；解析 graph 的 orphan、hub stub、fragile bridge、isolated community、phantom hub。
4. 对自由 Markdown 使用保守解析：没有可靠页面、证据或建议时，保留标题和摘要并把字段置空；不得从自然语言猜造来源页、严重性或置信度。
5. 基于相关报告的 mtime 使用短期内存缓存；mtime 改变即失效。
6. 不调用 LLM、不启动子进程、不写 Wiki、不访问 `raw/` 正文、不调用 PublishService、不要求 MySQL。
7. 解析失败记录脱敏日志，并返回 `parse_failed` 状态；不能令质量 API 崩溃。

### 3.4 路由与依赖注入

新增：

- `app/api/quality.py`
- `app/schemas/quality.py`
- 必要时 `app/main_dependencies.py` 的 `get_quality_report_service()`

修改：

- `app/main.py`：在 lifespan 注入 `app.state.quality_report_service`，并 `include_router(quality_router)`。
- `app/config.py`：只在需要时增加 `WIKI_BACKEND_QUALITY_STALE_AFTER_HOURS`，使用类型化默认值；同步 `.env.example` 与 README。

接口 description 要明确写出：数据来自最近一次 Agent 报告；语义内容需人工核对；本接口不会运行巡检或修复。

### 3.5 后端测试与完成标准

新增 fixture：正常、缺 health、过期 lint、过期 graph、损坏 Markdown、未知章节、无新鲜度快照。

新增测试：

1. `QualityReportService` 对每个 fixture 返回正确状态和计数。
2. 过期报告不会在 `tab_counts` 中伪装为当前结果。
3. 不完整矛盾项能安全返回，不会丢失标题也不会捏造 evidence/pages。
4. API 响应符合 Pydantic schema，且不泄露 Windows/DGX 绝对路径。
5. 质量服务在 fake 环境中不依赖 MySQL、LLM、PublishService。

Windows 验证：

```powershell
cd C:\job_docs\knowledge_base\mvc_sample\wiki-backend
.venv\Scripts\python.exe -m unittest discover -s tests
```

后端线程交付物：代码、测试、`.env.example`/README（如新增配置）、以及一份可供 Quartz 使用的 API fixture JSON。

## 4. 前端线程计划：`quartz`

前端线程以 `design/ui-prototypes/quality.html` 为验收对象；不等待真实 API 完成。先使用后端线程交付的 fixture，之后切换到同源 `/api/quality/latest`。

### 4.1 页面组件和静态骨架

修改 `.local-plugins/knowledge-ui/src/components/QualityPage.tsx`，保留 slug `quality`、`getKnowledgeObjects(props.allFiles)` 和既有导航，重建以下 DOM 区域及稳定 `data-*` 锚点：

| 原型区域 | Quartz 组件职责 | 必需锚点 |
|---|---|---|
| 页头与两个操作 | 标题、说明、报告查看、受控运行说明 | `data-quality-report`, `data-quality-run` |
| 四格状态带 | 先显示 loading，后由 API 填充 | `data-quality-snapshot` |
| 五个标签 | 全部/内容一致性/结构完整性/图谱质量/新鲜度与修复 | `data-quality-tab` |
| 左栏内容 | 分区、问题列表、结构表、新鲜度建议 | `data-quality-section`, `data-quality-finding` |
| 右栏证据 | 当前选中问题的两条证据、页面、建议和状态 | `data-quality-evidence` |
| 检查边界 | health/lint/graph 的可信范围，始终可见 | `data-quality-boundary` |
| 静态 metadata 补充 | 缺摘要/标签/更新时间；标明“构建期索引” | `data-quality-metadata` |

默认选择“全部发现项”与第一条内容一致性 finding；没有 finding 时右栏显示说明性空状态。页面首屏不显示原型内的假数据。

### 4.2 客户端交互脚本

在 `knowledge-ui` 中新增质量页客户端模块（命名按该插件现有构建约定确定），并由 `QualityPage.afterDOMLoaded` 返回脚本字符串。同步修改 `KnowledgePage.tsx`：将 `QualityPage.afterDOMLoaded` 加入当前只包含 Home/Library 的脚本组合。

脚本实现：

1. 监听 Quartz `nav` 事件，采用 `dataset.bound` 防止 SPA 重复绑定。
2. `fetch("/api/quality/latest", { headers: { Accept: "application/json" } })`；不写入硬编码 `127.0.0.1:8081`。
3. 使用 `textContent` 和安全 DOM 构造渲染所有报告文本；不得把 Lint Markdown 用 `innerHTML` 注入。
4. 标签点击只显示对应 `category` 的区块，`全部` 显示全部；tab count 来自 `tab_counts`。
5. 点击/键盘激活 finding 后更新右侧 evidence 面板的标题、两条证据、涉及页面、建议和状态。
6. `data-quality-report` 第一期开启页面内的结构化“报告来源/章节”说明，不请求或下载原始 Markdown。
7. `data-quality-run` 第一期开启说明：检查必须在受控运维流程中执行；按钮不得发起 POST。
8. API 请求失败、非 200、无效 JSON、`stale`、`missing`、`parse_failed`、`not_run` 均有不同且可理解的文案；不会显示绿色“通过”。

### 4.3 视觉与内容验收要求

必须遵循原型：

- 保留浅灰纸张背景、深墨绿活动导航、细灰色分隔线、宋体主标题、克制的状态色；不要改成卡片瀑布流、渐变仪表盘或大面积彩色图表。
- 顶部四格必须为横向分隔的巡检快照，不是四个浮动卡片。
- 左侧 finding 行显示严重性、标题、摘要和状态；选中行有明确但克制的底色/边界。
- 右侧 evidence 面板必须容纳两份来源证据、页面定位、涉及页面和“建议核对来源”。
- 结构完整性使用表格/行式结果；图谱和新鲜度使用分隔清晰的发现项/建议项。
- “自动发布结论”“原始资料事实验证”必须在检查边界中标为“本页不判断”。
- 小屏幕时右栏证据面板移动到内容区下方，五个标签允许横向滚动，不丢失操作或文本。

### 4.4 前端测试和构建

新增 `knowledge-ui` 单元测试，覆盖：

1. 五种 `check.state` 的展示文案。
2. finding 缺少 pages/evidence/recommendation 时的空状态。
3. graph 为 `stale` 时不展示历史风险数为当前结论。
4. API 错误时仍显示 `getKnowledgeObjects` 的 metadata gap，但 Agent 报告区块明确不可用。
5. 标签筛选、默认选中、切换 evidence、键盘访问和 SPA 重复初始化。

构建与验证：

```powershell
cd C:\job_docs\knowledge_base\mvc_sample\quartz\.local-plugins\knowledge-ui
npm.cmd test
npm.cmd run build

cd ..\..
CHAT_PROXY_URL=/api npx.cmd quartz build -d ..\llm-wiki-agent\wiki
```

提交 `src/` 与对应 `dist/`；不提交 `public/`、`node_modules/` 或 `.quartz/plugins/`。

前端线程交付物：组件、运行时脚本、测试、`dist/`、原型对照截图/人工视觉验收记录。

## 5. 联调与发布验收

### 5.1 Windows 联调

1. 使用后端 API fixture 测试前端全量状态，再接入本地 `GET /api/quality/latest`。
2. 在浏览器检查 `/quality` 只发出同源 `/api/quality/latest` 请求，且没有 POST/写操作。
3. 验证所有五个标签、列表选中、右侧 evidence、API 故障和过期报告状态。
4. 检查 `public/quality.html` 与它引用的最新哈希脚本，避免只构建 `dist/` 而忘记 Quartz build。

### 5.2 DGX

```bash
cd /home/dgx/Projects/knowledge_base_mkt/wiki-backend
.venv/bin/python -m unittest discover -s tests
curl --fail --silent --show-error http://127.0.0.1:8081/api/quality/latest
curl --fail --silent --show-error http://127.0.0.1:8080/api/quality/latest

cd /home/dgx/Projects/knowledge_base_mkt/quartz/.local-plugins/knowledge-ui
npm run build
cd ../..
CHAT_PROXY_URL=/api npx quartz build -d /home/dgx/Projects/knowledge_base_mkt/llm-wiki-agent/wiki
test -s public/quality.html
curl --fail --silent --show-error http://127.0.0.1:8080/quality > /dev/null
```

检查 `public/quality.html` 不包含 `/quartz/` 资源前缀；quality API 始终经 DGX Nginx 同源 `/api`。

### 5.3 ECS

```bash
curl -sS -D - -o /dev/null http://127.0.0.1:8080/api/quality/latest
curl -sS -D - -o /dev/null http://127.0.0.1:8080/quality
```

预期：`/api/quality/latest` 为 `X-Cache-Status: BYPASS`；静态 `/quality` 保留既有短缓存策略。不得影响 `/research_report_library/` 等现有路由。

## 6. 明确留待后续授权的工作

只有在用户明确允许改动 Agent 或部署独立受控 worker 后，才进入下一阶段：

1. 让 health/lint/graph/refresh 产生统一 JSON 快照，替代长期解析自由 Markdown。
2. 为巡检执行、refresh、heal 建立认证、角色权限、审计、可取消任务与人工确认流程。
3. 让质量页的“运行新一轮检查”成为真实后台任务入口；成功后记录报告版本和后续发布状态。

在此之前，`/quality` 的承诺仅是：**准确展示最近一次已有巡检报告及其边界，不自动修复知识库。**
