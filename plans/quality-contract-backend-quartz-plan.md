# 知识质量机器可读契约：wiki-backend + Quartz 开发计划

## 1. 状态、目标与范围

- 状态：待开发。
- 目标：把“知识质量”从 Quartz 的构建期元数据检查，扩展为一份由 `wiki-backend` 生成、持久化并通过同源 API 提供的质量快照；Quartz 负责展示质量范围、时效、问题队列和对应 Wiki 页面。
- 改动仓库：`wiki-backend`、`quartz`。
- 不改动仓库：`llm-wiki-agent`。它继续是 `wiki/`、`raw/`、`graph/` 的共享数据提供者；后端不得导入、调用或修改其 `tools/*.py` 源码。

本计划使用“质量检查”而不是“健康分数”。每一项结果必须说明其检查范围、证据来源和生成时间；未执行、输入过期或数据不完整时显示 `unknown` 或 `stale`，不得推断为通过或失败。

## 2. 当前事实与设计结论

### 2.1 当前职责

```text
llm-wiki-agent
  └─ 维护共享数据：wiki/、raw/、graph/

wiki-backend
  └─ 已经读取共享数据，管理 MySQL 任务，并通过 /api 提供业务能力

Quartz
  └─ 构建 wiki/ 静态页面；浏览器通过同源 /api 调用 wiki-backend
```

当前 Quartz 的 `QualityPage` 只从本次构建的 `props.allFiles` 统计摘要、标签和更新时间。这是可靠的静态元数据检查，但无法判断断链、图谱时效或语义矛盾。

`llm-wiki-agent/tools/health.py`、`lint.py` 和 `build_graph.py` 是有价值的参考实现，但不应成为本功能的运行时依赖：

- `health.py` 的检查是确定性的，可在后端原生复刻。
- `lint.py` 同时包含链接/图谱检查和 LLM 语义检查，且其语义输入范围需要在结果中明确记录。
- `build_graph.py` 的产物属于共享 `graph/` 数据；后端只读取其 `graph.json` 及可验证元数据，不执行该脚本。

因此，`wiki-backend` 是机器可读契约、任务管理和质量快照的唯一所有者；Quartz 不读取本地绝对路径、不执行 Python，也不解析 Markdown 报告正文来猜测数值。

### 2.2 非目标

- 不把质量任务绑定为每次入库都必须成功的发布前置条件。
- 不修改 `llm-wiki-agent` 的 `tools/`、提示词或 Wiki 生成流程。
- 不把 LLM 语义结果表述为全库事实、自动修复结论或总分。
- 不在浏览器直接访问 `127.0.0.1:8081`、`graph/` 文件系统或 `llm-wiki-agent` 路径。
- 不把质量报告写回 `llm-wiki-agent/wiki`；质量历史保存在 `wiki-backend` 的 MySQL 中。

## 3. 目标架构

```text
浏览器
  └─ Quartz /quality
       ├─ 构建期元数据检查（静态 HTML）
       └─ GET /api/quality/summary（运行时 JSON）
             └─ DGX Nginx 同源代理
                  └─ wiki-backend
                       ├─ MySQL：quality_jobs、quality_runs / findings
                       ├─ 只读 wiki/：结构、索引、日志、链接
                       ├─ 只读 graph/graph.json：图谱检查与时效
                       └─ app/llm_config.py：受控的语义检查

llm-wiki-agent
  └─ 仅继续提供共享 wiki/、raw/、graph/ 数据
```

质量任务分两类：

1. `structural`：确定性检查，不调用 LLM。成功发布后自动排队一次；也允许管理员手动触发。
2. `semantic`：链接/图谱结果加受控的 LLM 语义核对。默认只由管理员手动触发，不能随每次 ingest 自动执行。

`full` 是一次同时包含 `structural` 与 `semantic` 的人工任务。发布成功与 `structural` 任务失败互不回滚：发布仍由其既有验证决定，质量任务失败只会记录失败和错误摘要。

## 4. v1 机器可读契约

### 4.1 状态枚举

```text
检查状态：pass | warning | error | unknown | stale
任务状态：queued | running | succeeded | failed
任务范围：structural | semantic | full
问题严重度：info | warning | error
问题类别：metadata | structure | links | graph | semantic | data_gap
```

语义冲突、资料缺口和疑似笔误默认是 `warning`，不是 `error`；`error` 只用于可验证的硬失败，例如断链、无法读取必要输入或任务执行失败。

### 4.2 `GET /api/quality/summary`

该接口面向 Quartz，只返回每个范围最新的成功快照、当前时效和有限数量的高优先级问题。响应中的所有时间均为 UTC ISO 8601；所有页面路径均相对于 `wiki/`，使用 `/` 分隔符。

```json
{
  "schema_version": "v1",
  "generated_at": "2026-07-27T12:00:00Z",
  "publication": {
    "release_id": "<publish-job-id>",
    "published_at": "2026-07-27T11:55:00Z",
    "wiki_revision": "sha256:<manifest-hash>"
  },
  "checks": {
    "structural": {
      "status": "warning",
      "generated_at": "2026-07-27T11:54:00Z",
      "freshness": "fresh",
      "input": {
        "wiki_revision": "sha256:<manifest-hash>",
        "page_count": 147,
        "graph_built_at": "2026-07-27T10:00:00Z"
      },
      "summary": {
        "stub_pages": 0,
        "stale_index_entries": 0,
        "unindexed_pages": 1,
        "sources_without_ingest_log": 0,
        "broken_links": 0,
        "orphan_pages": 3,
        "sparse_pages": 8,
        "graph_status": "fresh"
      }
    },
    "semantic": {
      "status": "stale",
      "generated_at": "2026-06-23T08:00:00Z",
      "freshness": "stale",
      "coverage": {
        "sampled_page_count": 24,
        "sampled_paths": ["sources/example.md"],
        "model": "configured-main-model"
      },
      "summary": {
        "semantic_findings": 6,
        "data_gaps": 5
      }
    }
  },
  "findings": [
    {
      "id": "stable-finding-id",
      "category": "links",
      "severity": "warning",
      "rule_id": "orphan-page",
      "title": "页面缺少入站链接",
      "path": "sources/example.md",
      "evidence": "未发现来自其他 Wiki 页面的链接。",
      "recommendation": "从相关实体或概念页补充链接。",
      "check_scope": "structural"
    }
  ],
  "truncated": false
}
```

契约规则：

- `generated_at` 表示本份摘要接口的生成时间；每个检查还必须单独保留实际运行时间。
- `freshness` 由快照的 `wiki_revision` 与已发布版本的 `wiki_revision` 比较得出；不存在快照时为 `unknown`，不以文件修改时间代替。
- 图谱文件不存在、JSON 无效或图谱版本不同步时，图谱项为 `unknown` 或 `stale`，不得报“零问题”。
- `findings` 默认最多返回 20 条，以 `error`、`warning`、最新运行顺序排序；详情接口用于分页读取全部结果。
- `evidence`、`recommendation` 视为不可信文本。Quartz 必须以纯文本方式渲染，禁止注入 HTML。

### 4.3 任务与详情接口

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/api/quality/summary` | Quartz 质量页概览；无快照时也返回 `200` 和 `unknown`。 |
| `GET` | `/api/quality/findings?scope=&severity=&limit=&cursor=` | 分页读取问题队列。 |
| `GET` | `/api/quality/jobs` | 管理端查看近期质量任务。 |
| `GET` | `/api/quality/jobs/{job_id}` | 查看一个任务、覆盖范围和错误详情。 |
| `POST` | `/api/quality/jobs` | 创建 `structural`、`semantic` 或 `full` 任务，返回 `202 Accepted`。 |

`POST /api/quality/jobs` 是会读取大量文件及可能调用模型的写接口。DGX 与 ECS 必须将该路径纳入与 `/api/publish/` 同等级的管理保护：禁用缓存、限流，并在现有反向代理管理认证方案中显式覆盖它。Quartz v1 只读取摘要和问题详情，不提供“运行检查”按钮，避免把模型调用权限暴露给普通浏览者。

## 5. wiki-backend 实施计划

### 5.1 数据模型与存储

1. 新增 `app/schemas/quality.py`，使用完整 Pydantic 类型定义请求、任务、摘要、检查、覆盖范围与问题响应；为 API 增量新增字段，不改动现有 ingest/publish 响应。
2. 在 MySQL 初始化中新增：
   - `quality_jobs`：任务 ID、范围、状态、请求/开始/结束时间、输入 revision、错误摘要。
   - `quality_runs`：成功或失败的检查快照、关联 `quality_job_id`、可选 `publish_job_id`、各检查状态、输入元数据与摘要 JSON。
   - `quality_findings`：关联 run 的稳定 finding ID、类别、严重度、规则、相对路径、文本证据和排序字段。
3. 为 `quality_jobs(status, created_at)`、`quality_runs(scope, finished_at)`、`quality_findings(run_id, severity, id)` 建立索引；所有路径字段拒绝绝对路径和 `..` 越界。
4. 存储层读取 JSON 时必须经 Pydantic 校验；损坏的历史 JSON 只影响对应快照，接口返回 `unknown`，不得让 `/api/quality/summary` 整体 500。

### 5.2 纯检查内核

1. 新增不依赖 FastAPI/MySQL 的 `QualityAnalyzer`，输入为显式 `wiki_root`、`graph_root` 和不可变配置，输出 Pydantic 领域结果。
2. 在后端原生实现并单测以下确定性规则，行为可参考 agent 工具但不导入其代码：
   - 空页面/短页面；frontmatter 不计入正文长度。
   - `wiki/index.md` 与磁盘页面的双向同步。
   - `wiki/log.md` 对 source 页的 ingest 覆盖。
   - Wikilink 断链、孤儿页、低出链密度。
   - `graph/graph.json` 是否可读、是否为空、是否与当前 Wiki revision/page count 对应；可用时执行 hub stub、脆弱桥接和孤立社区检查。
3. 对 `graph.json` 缺失、时间未知、文件损坏、节点与页面映射不完整分别生成可区分的 findings 和 `unknown/stale` 状态。
4. 新增 `WikiRevision`：对参与检查的 Markdown 相对路径、大小和内容哈希生成稳定 manifest hash；不使用 Windows/DGX 绝对路径，也不把 `raw/` 纳入 revision。

### 5.3 语义检查内核

1. 语义检查使用 `wiki-backend/app/llm_config.py` 与后端自有 prompt，不读取或复制 `llm-wiki-agent/tools/lint.py` 的模型调用实现。
2. 新增结构化 LLM 输出 schema：每项必须含类别、严重度、标题、涉及页面、证据、建议；模型输出先经 Pydantic 验证，不合格输出使该语义任务失败并保留安全错误信息。
3. 不采用“目录顺序前 20 页”的隐式抽样。按 source/entity/concept/synthesis 分层采样，配置最大页数与单页最大字符数，并把实际采样路径、模型标识、提示词版本、截断信息写入 `coverage`。
4. 语义结果只能是 `semantic` 或 `data_gap` finding；不得自动修改 Wiki、自动判定事实真伪或覆盖确定性规则结果。
5. 新增配置项，包含语义开关、最大采样页数、单页文本上限、模型 token 上限和任务超时；默认语义任务不由 ingest/publish 自动触发。

### 5.4 服务、并发与发布关联

1. 新增 `QualityService` 及单独后台 worker，复用应用已有 `wiki_lock`，使分析快照与 ingest/synthesis 的 Wiki 写入互斥。
2. `structural` 任务在每次成功发布后自动排队；`semantic/full` 仅由受保护的管理 API 创建。
3. 扩展发布快照流程：在 `PublishService` 已创建的 Wiki snapshot 上计算 `WikiRevision`，并将该 revision 作为发布版本的可选新增字段持久化。结构检查应针对同一份 snapshot 运行，而不是针对随后可能变化的 live Wiki。
4. 结构检查异常不得阻断 Quartz build 或将已成功的发布回滚；它只产生失败的 quality run 和日志。发布失败的 snapshot 也可以保留质量记录，但必须标记 `publication_match=false`。
5. `GET /api/quality/summary` 以当前活跃发布版本为基准计算 freshness；如果 live Wiki 已改变但尚未发布，额外返回 `pending_publication=true`，前端显示“待发布变更，不代表当前站点”。

### 5.5 FastAPI 集成与测试

1. 新增 `app/api/quality.py`，在 `app/main.py` 注册路由并通过 app state 注入 `QualityService`；更新 `/docs` 的服务说明和 README API 列表。
2. 复用现有 `create_app(...)` 的依赖注入方式，支持 fake storage/fake quality service，避免 API 单测依赖真实 MySQL、真实 Wiki 或 LLM。
3. 后端测试至少覆盖：
   - 每条确定性规则的正常、无输入、损坏输入、边界路径。
   - revision 在内容变化、路径变化和无关文件变化时的预期行为。
   - 语义 LLM 输出有效、非法、超时与失败。
   - 任务状态机、并发/锁、发布成功或失败关联。
   - API 状态码、Pydantic 序列化、`unknown/stale` 和分页稳定性。
4. 完成后在项目虚拟环境运行：

   ```powershell
   .\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests
   ```

## 6. Quartz 实施计划

### 6.1 页面行为

1. 保留 [`.local-plugins/knowledge-ui/src/components/QualityPage.tsx`](../.local-plugins/knowledge-ui/src/components/QualityPage.tsx) 现有的构建期元数据区，不将其与后端质量快照混为一个数字。
2. 在其后增加“运行时质量检查”区：初始 HTML 使用骨架/加载状态；`afterDOMLoaded` 通过 `GET /api/quality/summary` 获取结果。
3. Quartz 的客户端导航会重复触发页面生命周期，脚本必须用 `data-bound` 去重，并在每次 `nav` 后重新定位本页容器；请求失败、非 2xx、未知 schema 版本和 JSON 解析失败都渲染可理解的 `unknown` 状态。
4. 页面不得提供质量任务启动按钮。v1 仅展示只读状态、问题和受影响 Wiki 页面链接。

### 6.2 UI 信息层级

```text
知识质量
├─ 构建期元数据完整性（现有，静态）
├─ 检查状态栏（运行时）
│  ├─ 结构与链接：状态、生成时间、是否匹配当前发布
│  ├─ 图谱：新鲜 / 已过期 / 未生成
│  └─ 语义核对：状态、抽样页数、生成时间
├─ 优先处理（最多 5 项）
│  └─ 严重度、类别、标题、涉及页面、简短建议
├─ 分项检查
│  ├─ 结构：短页、索引同步、日志覆盖
│  ├─ 链接：断链、孤儿、低出链密度
│  ├─ 图谱：hub stub、脆弱桥接、孤立社区
│  └─ 语义：待核对口径、疑似笔误、资料缺口
└─ 边界说明
   └─ 不显示总分；semantic 显示覆盖范围和非实时限制
```

视觉规则：`pass` 仅代表已完成且当前输入匹配；`warning` 代表有可处理项；`error` 代表确定性失败；`unknown/stale` 使用中性灰或琥珀色，不使用“健康/不健康”语义。页面应优先显示可点击的具体页面，而不是长篇原始 LLM 文本。

### 6.3 前端数据与安全

1. 新增一个小型纯 TypeScript 模块，定义 v1 响应类型、运行时收窄/验证、状态文案、UTC 时间格式化和相对 Wiki 路径到 Quartz URL 的映射。
2. 不信任 API 返回的 `title`、`evidence`、`recommendation`：使用 Preact 文本节点或 `textContent`，禁止 `innerHTML`。
3. 页面路径仅接受 API 的 Wiki 相对路径；先拒绝 `..`、绝对路径、反斜杠和非 `.md` 文件，再映射为 Quartz slug。无法安全映射时显示文本，不生成链接。
4. 不缓存 `/api/quality/*` 响应；继续沿用同源 `/api`，不新增浏览器直连后端地址或 CORS 例外。

### 6.4 前端测试与构建

1. 为质量契约 parser、新鲜度文案、路径映射和恶意文本转义添加 `knowledge-ui` 单测 fixture。
2. 覆盖 `pass/warning/error/unknown/stale`、无质量快照、过期语义快照、后端离线和不受支持 schema 版本。
3. 修改后必须构建插件，提交同步的 `src/` 与 `dist/`；不得提交 `node_modules/` 或 `public/`。

   ```powershell
   cd .local-plugins\knowledge-ui
   npm.cmd run test
   npm.cmd run build

   cd ..\..
   npm.cmd run check
   $env:CHAT_PROXY_URL="/api"
   npx.cmd quartz build -d ..\llm-wiki-agent\wiki
   ```

4. 使用 `npm.cmd run serve:integrated` 验证 `/quality` 经 `http://127.0.0.1:8080/api/quality/summary` 加载；确认静态元数据区在后端不可用时仍可正确显示。

## 7. 开发顺序与线程拆分

### 阶段 A：后端契约先行（wiki-backend 线程）

1. 确认并冻结本文的 v1 JSON schema、枚举、错误语义与安全边界。
2. 实现 schema、存储迁移、确定性分析内核和 `GET /api/quality/summary`。
3. 实现任务队列、发布关联、受保护 `POST /api/quality/jobs` 与语义检查。
4. 用 fixture 和 fake LLM 完成单测；提供一份稳定的 v1 JSON fixture 给 Quartz 线程。

后端交付门槛：所有单测通过，`GET /api/quality/summary` 在无质量记录时稳定返回 `200` + `unknown`，且不读取/修改 `llm-wiki-agent/tools`。

### 阶段 B：前端展示（Quartz 线程）

1. 依据冻结的 v1 fixture 实现类型收窄、状态栏、问题队列与边界说明。
2. 保留现有静态元数据检查，并在后端离线时优雅降级。
3. 编写单测，构建 `knowledge-ui/dist`，重建 Quartz。

前端交付门槛：`/quality` 不出现 `/quartz/` 资源前缀；所有 `/api/quality/*` 请求同源；无法读取质量 API 时不显示假通过或假失败。

### 阶段 C：联调、发布与回滚

1. 先部署后端，运行一次受保护的 `structural` 和 `semantic` 任务，验证 MySQL 质量历史与 API payload。
2. 再部署 Quartz，验证局域网和 ECS 入口下的 `/quality`、API 同源路径、缓存边界和移动端布局。
3. 配置 Nginx：`/api/quality/` 禁止缓存；`POST /api/quality/jobs` 复用管理保护与限流。
4. 回滚策略：前端可独立回滚到不请求质量 API 的旧插件；后端可保留质量表但下线路由。质量功能不得影响现有 chats、ingest、synthesis 和发布任务。

## 8. 最终验收清单

- [ ] 未修改 `llm-wiki-agent` 工作树、`tools/` 或 Wiki 内容。
- [ ] 后端只经配置的 `WIKI_AGENT_REPO_PATH` 读取 `wiki/`、`graph/`，不导入 agent Python 源码。
- [ ] `/api/quality/summary` 的路径、时间、状态和 schema 均符合 v1 契约。
- [ ] 未运行、失败、图谱缺失和过期语义检查均显示 `unknown/stale`，没有虚构的零问题或总分。
- [ ] 结构质量快照可关联到具体 Quartz 发布版本及 Wiki revision。
- [ ] 语义结果记录采样范围、模型与生成时间，且不会自动改写 Wiki。
- [ ] Quartz `/quality` 同时展示静态元数据和运行时质量结果，并能在 API 不可用时降级。
- [ ] 前端不注入不可信 API 文本，不生成越界页面链接。
- [ ] `knowledge-ui/src`、`knowledge-ui/dist`、后端单测、API 文档和两仓 README 均同步更新。
- [ ] DGX 与 ECS 对 `/api/quality/*` 不缓存；管理性 POST 接口已受保护和限流。
