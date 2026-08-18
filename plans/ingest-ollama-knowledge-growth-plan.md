# Ingest 本地 Ollama 与知识库增长治理计划

> 状态：待实施
>
> 目标环境：`wiki-backend` + 相邻 `llm-wiki-agent` Wiki + Quartz；默认目标模型为本地 Ollama `qwen3.6:35b`。
>
> 编写依据：截至 2026-08-17 的当前代码、`.env.example`、既有 Ingest 测试和已确认的 DGX 模型参数。当前工作区未提供真实 `.env`，本文不把示例配置写成已核验的生产运行值。本文是实施计划，不表示以下改动已经完成。
>
> 已确认运行参数：DGX 上 `qwen3.6:35b` 的实际 context window 为 65536 tokens。DeepSeek V4 Pro 使用独立的云端能力参数，不继承 Qwen 的 context 或 token 预算。
>
> 计划关系：`ingest-prompt-context-retrieval-remediation-plan.md` 中可复用的 Prompt 预算、确定性检索、诊断和测试方案已合并到本文。后续以本文作为 Ingest 模型切换、长文档和知识库增长治理的统一实施入口；原计划保留为问题分析记录，不再单独排期。

## 1. 目标与边界

### 目标

1. 在本地 Ollama 模型存在上下文和输出限制的前提下，保证长文档可以稳定入库。
2. 知识库规模增长后，Ingest 能检索真正相关的历史页面，而不是只依赖最近修改的页面。
3. 每条新增知识都尽可能能够回溯到原文证据，避免模型在合并旧知识时产生幻觉或覆盖已有内容。
4. Ingest 模型切换不影响 Chat、query、maintenance、lint 和 synthesis 的模型配置。
5. LLM、文件写入、Wiki 校验和 Quartz 发布之间具有清晰的成功边界，可失败、可重试、可恢复。

### 非目标

- 不修改 `llm-wiki-agent` 的 Python 源码。
- 不把 Ollama `11434` 暴露给浏览器、Nginx 或公网。
- 不通过单纯调大 `max_tokens` 解决长文档问题。
- 不在未完成离线评测前直接把生产 Ingest 全量切到本地模型。

## 2. 当前事实与主要问题

当前 Ingest 使用 `call_llm_main()`，实际配置来自全局 `WIKI_BACKEND_LLM_PROVIDER`、`WIKI_BACKEND_LLM_MAIN_MODEL`，调用位置在
`app/services/ingest_service.py::_call_llm_with_retry()` 和 `app/llm_config.py::call_llm_main()`。
当前代码默认值和 `.env.example` 的全局 provider/main model 是 `deepseek` / `deepseek-v4-pro`；真实部署 `.env` 需在 DGX 实施时再次核验。Chat 的 `model profile` 和 `FAST_MODEL` 不会改变 Ingest。

当前 Prompt 在 `app/services/ingest_service.py::_build_prompt()` 中拼接：

- 完整 `app/prompts/agent_instructions.md`；
- 完整 `wiki/index.md`；
- 完整 `wiki/overview.md`；
- 最近修改的 5 个 Source 页面全文；
- 当前转换后的文档全文；
- `app/prompts/ingest.md` 的完整 JSON 输出契约。

当前实际工作流为：

```text
POST /api/ingest/jobs
  -> 校验文件名、扩展名、大小、来源类型并保存到 raw/uploads
  -> MySQL 创建 queued 任务
  -> 进程内 Queue[int] 唤醒单个 ingest-worker
  -> 非 Markdown 转换；PDF 必要时执行 OCR；检查转换文本质量
  -> _build_prompt() 拼接完整源文档和固定 Wiki 上下文
  -> call_llm_main(max_tokens=WIKI_BACKEND_INGEST_LLM_MAX_TOKENS)
  -> JSON 解析；失败时使用原 Prompt 和失败响应再做一次 repair
  -> 在 wiki_lock 下直接逐文件写 Source/Entity/Concept/Overview/index/log
  -> 写后检查断链和未索引页面
  -> 标记 succeeded，再加入 Quartz publish 队列
```

失败任务会被标记为 `failed` 并删除该任务记录的上传源文件；转换生成的同名 Markdown、LLM debug response 和已完成的部分 Wiki 写入并不属于同一个可回滚事务。当前 `stage` 只有 `uploaded/converting/extracting/writing_wiki/validating/completed`，尚不能表达分块提取、检索、staging、`needs_review` 或回滚状态。

需要重点处理的问题：

| 优先级 | 问题 | 规模增长后的影响 |
| --- | --- | --- |
| P0 | Ingest 与其他内部任务共用全局 MAIN 模型配置 | 切换 Ollama 会连带改变 query、lint、maintenance 等行为 |
| P0 | 单一 `WIKI_BACKEND_INGEST_LLM_MAX_TOKENS` 不能表达不同模型的输入、输出和总 context 能力 | 切回 DeepSeek 时可能错误继承 Qwen 的 65536 context 限制，或切到 Qwen 时误用云端预算 |
| P0 | 没有输入 token 预算，`index`、`overview`、文档正文可能无限增长 | 本地模型 context overflow、响应变慢、输出截断 |
| P0 | 没有最终 Prompt 的 token 重算和硬校验 | 估算误差或模板变化可绕过局部预算，直到 provider 才报错 |
| P0 | 一次调用同时生成 Source、Entity、Concept、Overview 和 JSON | `qwen3.6:35b` 更容易达到输出上限，失败时难以定位和重试 |
| P0 | 只取最近 5 个 Source，不做相关性检索 | 历史上真正相关的 Entity/Concept/Source 不会进入上下文 |
| P0 | `index.md` 只应提供目录元数据却被整篇发送 | 索引随页面数量线性增长，挤占真正证据的上下文预算 |
| P0 | Entity/Concept/Overview 可由模型整体覆盖 | 知识库越丰富，误删除和旧知识丢失风险越高 |
| P0 | 校验异常只写入 `validation`，仍可能标为 `succeeded` | 断链、未索引、无证据内容可能继续发布 |
| P1 | JSON repair 会重复完整原 Prompt | 已超长的请求会在修复阶段再次超限 |
| P1 | Wiki 多文件逐个原子写入，没有整体 staging/回滚 | 中途失败会留下半成品 Wiki |
| P1 | 任务队列是进程内 `Queue[int]` | 重启后 queued/running 任务无法可靠恢复 |
| P1 | 失败时删除原始上传文件 | 本地模型调参和失败复现困难 |
| P1 | 没有 source/content hash 和版本语义 | 同名文档更新、重复文档和增量重入库难以区分 |
| P1 | scheduled 冲突分支写 `state="skipped"`，MySQL storage 只接受 succeeded/failed | 重复定时来源可能导致同步任务异常退出 |
| P2 | 缺少模型、token、证据覆盖和 diff 指标 | 无法判断 Qwen 是否达到 DeepSeek 的质量水平 |

## 3. 目标架构

```text
上传/定时源
  -> 文件安全校验、hash、稳定快照
  -> durable ingest job
  -> Markdown 转换与文档质量门
  -> 按章节/页/语义分块，并限制每块 token
  -> 任务快照中的 Ingest profile 分块事实提取（Qwen 或 DeepSeek）
  -> 从源文档确定性提取检索特征
  -> 将允许参与知识推理的 Wiki 页面按章节建立候选
  -> 确定性相关页面检索（标题/实体/关键词/链接邻居，后续可加 BM25 或 embedding）
  -> 按 profile 计算 PromptBudget 并选择有界 Wiki 章节
  -> 渲染最终 Prompt 后重新计数并执行硬校验
  -> 聚合 Source 草稿与更新提案
  -> 确定性 schema、证据、链接、路径、diff 和 token 校验
  -> staging manifest
  -> Wiki 原子提交/回滚
  -> succeeded 或 needs_review
  -> 成功后进入 Quartz 发布队列
```

分块提取、相关页面检索和 Wiki 提交必须有独立阶段状态。模型调用可以受控并发，但 Wiki commit 必须在共享锁和一致性检查下串行完成。

P0 的依赖顺序固定为：先建立 Ingest profile 和任务快照，再实现 token 估算与硬预算，然后实现相关 Wiki 检索，最后接入长文档分块提取和写入质量门。没有 profile 的 context 能力就无法正确计算预算；没有硬预算就不能安全启用检索或向 Ollama 发送长文档。

## 4. P0：切换 Ollama 前必须完成

### 4.1 增加受控的 Ingest 模型档案

修改：

- `app/config.py`
- `app/llm_config.py`
- `app/schemas/ingest.py`
- `app/storage/mysql.py`
- `.env.example`
- `tests/test_llm_config.py`
- `tests/test_startup_dependencies.py`
- `tests/test_ingest_api.py`
- `tests/test_ingest_service.py`

不要开放可任意组合的 Ingest provider、model、`api_base` 和 token 参数。增加独立于 Chat profile 的服务器端 Ingest 白名单：

```env
WIKI_BACKEND_INGEST_MODEL_PROFILE_DEFAULT_ID=local-qwen3.6-35b-direct
WIKI_BACKEND_INGEST_MODEL_PROFILE_ENABLED_IDS=local-qwen3.6-35b-direct,deepseek-v4-pro
```

第一版至少提供两个内部档案：

| Ingest profile ID | provider | model | reasoning | 用途 |
| --- | --- | --- | --- | --- |
| `local-qwen3.6-35b-direct` | `ollama_chat` | `qwen3.6:35b` | `none` / `think=false` | 默认本地入库、shadow 和低风险自动任务 |
| `deepseek-v4-pro` | `deepseek` | `deepseek-v4-pro` | provider default | 高风险文档、复杂冲突、基准和显式回退 |

复用现有 `app/llm_config.py::LLMProfile` 和 `call_llm_profile()`，但不要直接复用 Chat 的公开 `ModelProfileService`、Chat token 常量或浏览器选择状态。新增 `resolve_ingest_llm_profile(profile_id)` 和等价的专用调用入口。每个档案由服务端固定 provider、模型、连接来源、token/context 能力、温度和 reasoning 策略。

要求：

- Ingest 切换 Qwen/DeepSeek 时，Chat、query、lint、maintenance 和 synthesis 的模型不变。
- `api_key` 对 Ollama 不传入。
- `api_base` 保持根地址，不追加 `/v1`。
- DeepSeek 使用 `WIKI_BACKEND_DEEPSEEK_API_KEY` 和 `WIKI_BACKEND_DEEPSEEK_API_BASE`；Qwen 使用 `WIKI_BACKEND_OLLAMA_API_BASE`，不重复增加 Ingest 专属密钥或地址。
- direct/`think=false` 是 Qwen 的首期生产策略；thinking 仅作为后续低置信度复核实验，不作为默认 Ingest 档案。
- 通过实际 DGX Ollama 请求验证 `model`、`think`、`finish_reason` 和最终 `content`。

### 4.2 每个模型独立配置 context、输入和输出预算

单一共享的 `WIKI_BACKEND_INGEST_LLM_MAX_TOKENS` 只能表达输出上限，不能表达多模型能力。目标 `IngestLLMProfile` 至少包含：

```text
context_window
max_input_tokens
max_output_tokens
context_safety_margin
temperature
reasoning_effort
```

必须满足：

```text
max_input_tokens + max_output_tokens + context_safety_margin <= context_window
```

Qwen 首期预算固定为：

| 参数 | 值 |
| --- | ---: |
| `context_window` | 65536 |
| `max_input_tokens` | 49152 |
| `max_output_tokens` | 8192 |
| `context_safety_margin` | 8192 |
| `temperature` | 0.1 |
| `reasoning_effort` | `none` |

`8192` 是 Qwen 的合理初始输出预算，不因模型 context 为 65536 就直接调到 16384。只有 Golden Corpus 或真实 shadow 任务反复出现 `finish_reason=length`，且输出内容本身确有保留价值时，才试验 12288；此时输入预算必须同步降到不超过 45056。长期方案仍是分块和分阶段生成，不能以调大单次输出替代结构改造。

DeepSeek V4 Pro 必须使用独立的能力记录：

- `context_window` 以当前云端 API 的实际能力验证为准；
- `max_output_tokens` 以 provider 实际允许的输出上限验证为准；更大 context 不等于更大输出上限；
- `max_input_tokens` 根据 DeepSeek 自身 context、输出预留和安全余量计算；
- 不得继承 Qwen 的 65536 context、49152 输入预算或 8192 安全余量；
- 即使云端允许更大输出，分阶段改造后常规单次输出仍优先控制在 4096～8192，将更大的 context 主要用于相关证据输入。

现有 `WIKI_BACKEND_INGEST_LLM_MAX_TOKENS=8192` 在迁移期可以作为兼容字段，但不再作为所有 profile 的共同最终值。启用新 profile 配置后，有效预算优先级为 profile-specific override、profile 固定默认；旧变量只服务于尚未迁移到 profile 的兼容路径。完成所有部署迁移后再决定是否移除旧变量。

需要在 DGX 上继续记录 `prompt_eval_count`、`eval_count`、`eval_duration`、GPU/CPU offload、冷启动以及 context overflow、输出截断、OOM 和超时的实际错误形式。

### 4.3 支持按任务切换并持久化模型快照

`POST /api/ingest/jobs` 增加可选的 multipart 字段：

```text
model_profile_id=local-qwen3.6-35b-direct
```

不传时使用 `WIKI_BACKEND_INGEST_MODEL_PROFILE_DEFAULT_ID`。浏览器只能提交已启用的 Ingest profile ID，不能提交 provider、model、`api_base`、API key、context 或 token 参数。未知/未启用档案返回 `422`；已启用但当前不可用的档案返回 `503`，并且不保存上传文件、不创建 queued job。首期不得静默从本地模型回退到云端，避免本地文档意外发送给 DeepSeek。

创建任务时必须把实际执行快照写入 `ingest_jobs`：

```text
llm_profile_id
llm_provider
llm_model
llm_reasoning_mode
llm_context_window
llm_max_input_tokens
llm_max_output_tokens
prompt_version
```

不得持久化 API key、完整 Prompt 或 reasoning 原文。worker 必须按 job 快照执行，不能在任务排队后重新读取当前默认 profile，否则默认模型变更会让已排队任务悄悄换模型。Scheduled Ingest 默认继承系统 Ingest profile；若后续需要独立选择，再增加受控的 `WIKI_BACKEND_SCHEDULED_INGEST_MODEL_PROFILE_ID`。

### 4.4 Token 估算、PromptBudget 和最终硬校验

修改：

- `app/config.py`
- `app/services/ingest_service.py`
- 新增 `app/services/ingest_prompt_budget.py` 或职责等价模块
- `app/prompts/ingest.md`
- `.env.example`
- 新增 `tests/test_ingest_prompt_budget.py`
- `tests/test_startup_dependencies.py`
- `tests/test_ingest_service.py`

#### 4.4.1 预算对象和硬约束

增加只负责计数和分配、不读取文件也不调用 LLM 的 `PromptBudget`。至少记录：

```text
context_window_tokens
max_input_tokens
output_reserved_tokens
safety_margin_tokens
fixed_prompt_tokens
source_tokens
wiki_context_budget_tokens
estimated_total_input_tokens
estimator_strategy
```

每次 LLM 调用前必须同时满足：

```text
estimated_total_input_tokens
  = fixed_prompt_tokens
  + source_tokens
  + selected_wiki_context_tokens
  + prompt_wrapper_tokens

estimated_total_input_tokens <= profile.max_input_tokens

estimated_total_input_tokens
  + profile.max_output_tokens
  + profile.context_safety_margin
  <= profile.context_window
```

预算算法：

1. 先计算精简 instructions、输出 schema、标签和模板包装的固定开销。
2. 计算当前源文档或当前 chunk 的输入开销。
3. 从 profile 的 `max_input_tokens` 中扣除上述开销，得到本次 Wiki 上下文可用预算。
4. Wiki 上下文还要受 profile 的 `wiki_context_max_tokens` 上限约束，不能因为云模型 context 更大而无界扩张。
5. 检索结果按完整章节逐个装入预算；不得从句子、表格行或代码围栏中间截断。
6. `render_prompt()` 完成后必须对最终字符串重新计数；最终校验失败时零次调用 LLM，并返回稳定的预算错误分类。

`WIKI_BACKEND_INGEST_LLM_MAX_TOKENS` 只表示当前旧路径的输出上限，不能继续充当 context window。新的 context、输入、输出、安全余量和 Wiki 上下文上限由 Ingest profile 固定。可仅把确有运维价值的候选上限暴露为环境变量，例如：

```env
WIKI_BACKEND_INGEST_RETRIEVAL_TOP_K=12
```

不要直接沿用旧检索计划中的 `WIKI_BACKEND_INGEST_LLM_CONTEXT_WINDOW_TOKENS=32768` 或将 `WIKI_BACKEND_INGEST_WIKI_CONTEXT_MAX_TOKENS=6000` 设为所有模型共享值；这会重新制造 Qwen 与 DeepSeek 共用能力预算的问题。

#### 4.4.2 TokenEstimator

定义可替换的 `TokenEstimator` 协议：

- provider/model 有已验证 tokenizer 时使用真实 tokenizer；
- LiteLLM 无法识别本地 Qwen tokenizer 时，使用经 DGX 样本校准的保守估算，不把字符数直接当 token 数；
- 保守估算必须留足 profile safety margin，并记录 `estimator_strategy`、provider 和 model；
- 日志不得记录完整 Prompt、原文或 token；
- 单元测试注入确定性的 fake estimator，避免依赖联网 tokenizer 或不同版本词表。

字符估算只能作为可观察的 fallback。切换 tokenizer、模型 tag 或 Prompt 版本后，应在 Golden Corpus 上重新比较估算值与 provider 返回的 `prompt_eval_count` 或等价 usage 数据。

#### 4.4.3 精简固定 Prompt

当前 `app/prompts/agent_instructions.md` 按项目约定只能同步自 `llm-wiki-agent/AGENTS.md`，不能在该文件中混入或删改其他工作流规则。Ingest 专用精简规则应放入独立 Prompt 片段或直接重构 `app/prompts/ingest.md`，只保留：

- Source/Claim/证据提取职责；
- 当前阶段需要的 JSON schema；
- 路径、Frontmatter、引用和禁止臆造约束；
- 当前 chunk 和相关 Wiki 章节。

Query、Lint、Graph 等与本次结构化调用无关的说明不得继续占用 Ingest Prompt。Prompt 内容和 schema 变化必须更新 `prompt_version`。

### 4.5 确定性相关 Wiki 检索替换“最近 5 篇”

修改：

- `app/services/ingest_service.py`
- 新增 `app/services/wiki_context_retriever.py` 或职责等价模块
- 复用 `app/services/wiki_page_policy.py`
- `app/prompts/ingest.md`
- `tests/test_ingest_service.py`
- 新增 `tests/test_wiki_context_retriever.py`

#### 4.5.1 候选范围和分节

检索范围至少包含：

- `wiki/sources/*.md`；
- `wiki/entities/*.md`；
- `wiki/concepts/*.md`；
- `wiki/overview.md` 的分节内容。

`wiki/index.md` 只用于标题、路径和摘要等目录元数据，不再默认把全文加入 Prompt。继续沿用 `iter_knowledge_pages()` 的知识页策略，明确排除 `log.md`、maintenance/health/lint 报告和 graph 等运行产物。

候选解析规则：

1. 读取 Frontmatter 的 `title/type/tags/sources` 和页面内显式 wikilink。
2. 优先按 `##`、`###` 标题切成章节；超长章节再按段落切分。
3. 每个候选保存 Wiki 相对路径、页面标题、章节标题、正文、页面类型和估算 token。
4. 不从句子、Markdown 表格行或代码围栏中间切分；单个候选仍超预算时明确跳过并记录原因。

#### 4.5.2 查询特征与确定性排序

从当前源文档确定性提取查询特征：文件名、Frontmatter、一级/二级标题、高频中英文术语、显式实体名和 `[[Wikilinks]]`。首期不调用 LLM 改写检索 query，避免检索本身增加成本、随机性和失败点。

首期相关度按以下信号组合，具体权重写成代码常量并由测试锁定，不全部暴露为环境变量：

1. 标题、slug、tag 或显式 wikilink 精确命中：最高权重。
2. 页面标题和章节标题词项重合：高权重。
3. 中文字符 n-gram、英文规范化关键词和正文词项重合：基础权重。
4. 已命中页面的一跳 wikilink 邻居：小幅加权，不能替代直接证据。
5. Source/Entity/Concept/Overview 类型多样性：在同等相关度下避免单一页面类型垄断。
6. `mtime` 只作为同分项的末级 tie-break；最终再以规范化相对路径保证稳定顺序。

相同 Wiki 快照、相同源文档和相同配置必须得到相同排序。

#### 4.5.3 选择规则不是只有 Top-K

`top_k` 只是候选数量上限，最终入选还必须同时满足：

- 最低相关度阈值；
- `PromptBudget` 分配的总 token 上限；
- 单页章节数量上限和单章节上限；
- 页面类型/来源多样性约束；
- 每个候选必须完整装入剩余预算。

不得为了凑满 Top-K 填入低相关页面。空 Wiki 保持“首次 Source”语义；Wiki 非空但没有合格候选时，在 Prompt 中使用明确的“未检索到相关历史页面”占位，不得回退为“最近 5 篇”或伪造 overview。

向 Prompt 提供的上下文格式至少包含来源路径和章节标题：

```text
## wiki/entities/Example.md
### Relevant section
...
```

检索或解析自身异常时应使任务明确失败，不得回退到无界全文上下文。

#### 4.5.4 可观察性与敏感信息边界

每次 Ingest 至少记录结构化诊断：

- profile ID、provider、model、Prompt 版本和 estimator strategy；
- context window、输出预留、安全余量；
- fixed/source/wiki/final input token 估算；
- 候选数、合格数、入选数；
- 入选页面相对路径、章节标题、分数和选择/跳过原因；
- 最终预算是否通过以及 provider usage/finish reason。

模型和预算快照按 4.3 持久化；检索明细首期可以只写结构化日志，确认字段稳定后再决定是否持久化摘要。不得记录完整 Prompt、页面正文、源文档、API key 或 reasoning 原文。

### 4.6 长文档分块与分阶段提取

修改：

- `app/services/ingest_service.py`
- 新增源文档准备/分块模块或职责等价模块
- `app/prompts/ingest.md` 及分块提取 Prompt
- `tests/test_ingest_service.py`

实施要求：

1. 建立精简的 Ingest 专用 instructions，移除 Query、Lint、Graph 等无关工作流文本。
2. 由 4.4 的 `PromptBudget` 统计当前 chunk、候选 Wiki 章节、schema 和 instructions 的预算。
3. 以标题、页面、表格和段落边界优先切块；不能静默截断原文。
4. 每个块保留文档相对位置，例如页码、章节、表格标题。
5. 分块输出只返回结构化事实，不直接生成整套 Wiki 文件。
6. 超预算、空块、乱码块和 OCR 低质量块进入明确失败分类。

最低分块结果契约：

```json
{
  "section": "原文章节或页码",
  "claims": [
    {
      "claim": "结构化事实",
      "evidence_text": "原文证据摘录",
      "source_location": "page 12 / section 3.2",
      "entities": [],
      "concepts": []
    }
  ]
}
```

验收：长 Markdown 和长 PDF fixture 不得把超预算全文直接发送给模型；每个 chunk 都可以单独重试并保留失败原因。

若单个不可再分的源片段连同固定 Prompt 已超出预算，必须在调用 LLM 前以稳定分类 `ingest_source_context_too_large` 失败；不得依赖 provider 截断。分块事实汇总阶段同样使用独立 PromptBudget，不能把所有 chunk 结果无界拼回一次请求。

### 4.7 限制整体覆盖并增加证据门

模型不再默认返回完整的 Entity/Concept/Overview 文件，而是返回受控更新操作或草稿。

后端提交前必须检查：

- Claim 的 `evidence_text` 能在源文档中匹配；
- Source Frontmatter、slug 和输出路径合法；
- 不允许删除已有页面内容，除非是显式人工批准的操作；
- Entity/Concept 更新有来源 slug 和证据；
- Overview 大 diff 默认进入 `needs_review`；
- 断链、未索引、空页面、placeholder、schema 错误不得进入 `succeeded`。

### 4.8 P0 实施顺序和每阶段验收

#### 阶段 0：用失败测试固定当前问题

1. 构造超大 `index.md` 和 `overview.md`，证明旧 Prompt 随知识库大小线性增长。
2. 构造 5 篇最近但无关的 Source 和 1 篇较旧但高度相关的 Source，证明旧实现选择错误上下文。
3. 构造相关 Entity/Concept，证明旧实现不会读取这些页面。
4. 固定当前 Ingest 与全局 MAIN provider/model 耦合、最终 Prompt 无硬校验的事实。

验证：新增测试在旧实现上因目标缺陷失败，而不是因为 fixture 或 mock 错误。

#### 阶段 1：建立专用 profile 和任务快照

按 4.1～4.3 完成 Ingest profile、独立能力预算、API 白名单和 job 快照。先保持现有单次 Prompt 生成逻辑不变，只解除模型配置耦合。

验证：同一测试进程内分别创建 Qwen、DeepSeek job，worker 使用创建时快照；切换 Ingest 默认 profile 不改变 query、maintenance、lint、synthesis 或 Chat 档案。

#### 阶段 2：实现 PromptBudget

1. 实现 `TokenEstimator` 接口和确定性 fake。
2. 实现 profile 驱动的 `PromptBudget` 和跨字段校验。
3. 精简 Ingest 固定 Prompt。
4. 在每个实际 LLM 调用前渲染并重算最终 Prompt。
5. 增加稳定的输入超限错误分类和结构化预算日志。

验证：超限时 LLM caller 调用次数为 0；无论 `index.md`/`overview.md` 增长多少，最终请求都不越过任务 profile 的硬限制。

#### 阶段 3：实现相关页面检索

1. 实现知识页枚举、Frontmatter 解析和 Markdown 章节切分。
2. 实现查询特征提取和确定性混合词法评分。
3. 实现相关度阈值、类型多样性、每页上限和稳定排序。
4. 在 PromptBudget 内选择完整相关章节。
5. 删除“最近 5 篇”及无界全文回退逻辑。

验证：旧但相关的页面稳定排在最近但无关页面之前；Entity/Concept/Overview 的相关章节可被选中，新增无关页面不显著改变结果。

#### 阶段 4：接入长文档分块、证据门和分阶段生成

按 4.6～4.7 将源文档切块事实提取、相关 Wiki 上下文、聚合和受控更新串成完整流水线。每个阶段使用自己的 schema 和预算，不把所有中间结果重新无界拼接。

验证：长 Markdown、文本型 PDF 和扫描 PDF fixture 均不会把超预算全文发送给模型；任一 chunk 失败可定位、可重试，且不会产生半套正式 Wiki 结果。

#### 阶段 5：文档和 DGX shadow 验证

同步 `.env.example`、README 和现有 Ingest 流程文档，明确：

- `WIKI_BACKEND_INGEST_LLM_MAX_TOKENS` 是旧路径输出预算，不是 context window；
- `index.md`/`overview.md` 不再默认全文进入 Prompt；
- Wiki 上下文来自预算内的相关章节；
- Qwen 和 DeepSeek 使用独立 profile 能力；
- 超限、低证据和冲突的失败/`needs_review` 语义；
- Ingest 成功与 Quartz 发布成功仍是两个状态。

先在临时 Wiki 或 shadow/staging 上双跑，再按第 6 节的 Golden Corpus 指标决定是否切换默认模型。

## 5. P1：知识库规模化前完成

### 5.1 JSON repair 改为精简修复

当前 repair 会把完整原 Prompt 和失败响应再次发给模型。改为只发送：

- 精简 JSON schema；
- 失败响应；
- “只修复结构，不增加新事实”的规则。

截断、上下文超限、schema 不兼容不应继续执行同一种 repair。修复调用仍需要独立 token 和超时预算。

Repair 前也要执行最终 Prompt 硬校验。失败响应如果超过修复预算，应保存受控诊断并直接失败，不能静默截掉 JSON 尾部后声称修复成功。

### 5.2 Staging、manifest 和回滚

所有 Source/Entity/Concept/Overview/index/log 变更先写入 job 专属 staging 目录，并生成：

- 原文件 hash；
- 新文件 hash；
- 创建/更新/删除列表；
- 证据与来源映射；
- 校验结果；
- 模型和 Prompt 版本。

只有全部校验通过后才在 `wiki_lock` 下提交。提交前重新检查原文件 hash，防止并发修改覆盖用户变更。进程崩溃后保留可清理的 staging 状态。

### 5.3 持久化任务队列

将进程内 `Queue[int]` 演进为 MySQL claim/lease：

```text
queued -> running + lease_until -> succeeded/failed/needs_review
```

增加 attempt、worker、heartbeat、lease 过期恢复和重试原因。仍然可以保持单个 Wiki commit worker，避免多个进程同时写 Wiki。

### 5.4 内容 hash 与版本

`ingest_jobs` 和 scheduled source 增加：

- `content_sha256`；
- `source_identity`；
- `source_version`；
- `supersedes_job_id`；
- 转换结果 hash。

区分同名同内容、同名内容变化、不同文件名同内容和显式 re-ingest，不再只依赖文件主名。

### 5.5 失败文件保留策略

失败源文件进入 quarantine 并设置保留期，不要立即删除唯一原件。LLM debug response 需要：

- 受控访问；
- 保留期限；
- 自动清理任务；
- 日志中只记录诊断 ID，不记录完整 Prompt、原文或密钥。

### 5.6 修复 scheduled `skipped` 状态

当前 `app/services/scheduled_ingest_service.py` 在重复文档分支使用 `state="skipped"`，而 `app/storage/mysql.py::complete_scheduled_ingest_source()` 只接受 `succeeded` / `failed`。

需要统一状态契约：要么为 storage 增加 `skipped`，要么把重复视为一个独立的终态并同步 schema、统计、恢复逻辑和测试。必须补充真实 storage/fake storage 双覆盖测试。

## 6. P2：性能、评测和运营

### 6.1 建立 Golden Corpus

准备至少 20～50 篇代表性文档，覆盖：

- 普通 Markdown；
- 长 PDF；
- 扫描 PDF；
- 表格和多栏文档；
- 同一 Entity 的多次更新；
- 新旧内容冲突；
- 重复文件和版本更新；
- 超长和 OCR 低质量输入。

对 DeepSeek V4 Pro、DeepSeek V4 Flash 和 Qwen direct 分别记录：

- 任务成功率；
- JSON 完整率；
- 截断率；
- 证据覆盖率；
- Claim 准确率；
- 断链和未索引数量；
- 意外覆盖/删除数量；
- 首 token、总耗时、tokens/s；
- GPU/CPU offload；
- 重试次数和内存/OOM 错误。

### 6.2 灰度和回退

推荐顺序：

1. Qwen 只做 shadow/staging，不写正式 Wiki。
2. Qwen 与 DeepSeek 对同一 Golden Corpus 双跑并比较 diff。
3. Qwen 进入 `needs_review`，人工确认低风险文档。
4. Qwen 成为默认 Ingest，DeepSeek Pro 只处理超限、冲突和低置信度任务。
5. 连续观察截断率、证据覆盖率和回滚率后再关闭云端 fallback。

任何模型切换都不能以“HTTP 200”或“任务进入 succeeded”作为唯一成功依据。

## 7. 测试与验证要求

### 单元/服务测试

- 专用 Ingest 模型配置和 provider 隔离。
- Qwen 与 DeepSeek profile 分别使用自己的 context、输入、输出和安全余量；切换 DeepSeek 时不继承 Qwen 的 65536 限制。
- API 只能选择已启用的 Ingest profile ID；未知、禁用和不可用档案在文件落盘前失败。
- queued job 始终按创建时持久化的模型快照执行，不受后续默认 profile 变更影响。
- Ollama direct 模式不发送云端 API key，并验证 `think=false` 或等价参数。
- Prompt 预算覆盖空 Wiki、小 Wiki、10,000 条 index、超长 overview、恰好在边界和超出 1 token；非法 profile 预算在启动或解析时失败。
- tokenizer 不可用时使用可观察的保守 fallback；fake estimator 结果稳定。
- 最终 Prompt 重算能阻止模板包装导致的超限，超限时 LLM caller 调用次数为 0。
- 分块边界、表格、页码和 chunk 独立重试。
- 较旧但标题精确匹配的 Source 胜过最近无关 Source。
- Entity、Concept 精确匹配；`overview.md` 只选中相关章节；完整大 index/overview 不进入 Prompt。
- 中英文混合关键词、中文字符 n-gram、显式 wikilink 和一跳邻居按固定规则排序。
- 相同分数时结果稳定；单一大页面不能占满预算；低相关候选不会为了凑满 Top-K 被加入。
- `log.md`、runtime report、maintenance/health/lint 和 graph 产物不参与检索。
- 最终 Prompt 保留选中页面路径和章节标题；没有合格候选时不回退最近 5 篇。
- evidence grounding、schema、路径、slug、链接和 diff 门禁。
- JSON repair 不重复完整 Prompt。
- staging 提交、hash 冲突和失败回滚。
- queued/running lease 恢复。
- scheduled `skipped` 状态真实 storage 行为。

### DGX 实机验证

```bash
uname -m
.venv/bin/python --version
.venv/bin/python -m unittest discover -s tests
curl --fail --silent --show-error http://127.0.0.1:8081/api/health
```

需要额外验证实际 Ollama：

- `qwen3.6:35b` tag 存在；
- direct 请求产生最终 `content`；
- context overflow、OOM、超时可分类；
- 预热后交替运行至少 3 次再比较性能；
- 不向浏览器暴露 `11434`。

DeepSeek 和 Ollama 使用同一批代表性文档分别验证：

- 最终 Prompt 均位于各自 profile 预算内；
- provider usage 与本地估算偏差位于约定安全余量内；
- 旧的相关页面能够召回，新增无关页面不会明显改变结果；
- Source Claim 与原文证据可对应，且 Entity/Concept/Overview 不发生意外整体覆盖；
- 切换模型不会改变任务创建时已持久化的执行快照。

涉及真实 Wiki、真实 MySQL 或真实 LLM 的测试，先使用隔离目录、fake storage、fake LLM 或 shadow 模式；只有明确批准后才执行生产知识库导入。

### 建议验证命令

Windows：

```powershell
.venv\Scripts\python.exe -m unittest tests.test_ingest_prompt_budget -v
.venv\Scripts\python.exe -m unittest tests.test_wiki_context_retriever -v
.venv\Scripts\python.exe -m unittest tests.test_ingest_service -v
.venv\Scripts\python.exe -m unittest tests.test_llm_config tests.test_ingest_api tests.test_startup_dependencies -v
.venv\Scripts\python.exe -m unittest discover -s tests
```

DGX：

```bash
.venv/bin/python -m unittest tests.test_ingest_prompt_budget -v
.venv/bin/python -m unittest tests.test_wiki_context_retriever -v
.venv/bin/python -m unittest tests.test_ingest_service -v
.venv/bin/python -m unittest discover -s tests
curl --fail --silent --show-error http://127.0.0.1:8081/api/health
curl --fail --silent --show-error http://127.0.0.1:8080/api/health
```

真实 Ingest 验证必须使用临时或明确可回滚的 Wiki 副本；执行前确认 LLM、MySQL、Wiki 写入和 Quartz publish 的副作用范围。

## 8. 完成标准

本计划完成至少应满足：

1. Ingest 可以按任务选择 Ollama Qwen 或 DeepSeek V4 Pro，不改变其他内部任务模型。
2. Qwen 使用已确认的 65536 context 和 49152/8192/8192 输入、输出、安全余量；DeepSeek 使用独立验证的能力预算，不继承 Qwen 限制。
3. 每个任务持久化安全的模型、预算和 Prompt 版本快照，worker 严格按快照执行。
4. 长文档会分块，超出预算不会静默截断或直接发送全文。
5. 最终 Prompt 在每次 LLM 调用前重算并满足 profile 输入、输出和安全余量约束；超限时零次调用 LLM。
6. `index.md` 不再全文进入 Prompt，`overview.md` 仅按相关章节参与；不存在“最近 5 篇 Source”回退。
7. 历史相关 Source/Entity/Concept/Overview 章节能稳定胜过最近但无关页面，检索范围、分数和预算可观察。
8. 每条正式 Claim 有可验证原文证据，质量门不通过时为 `needs_review` 或 `failed`。
9. Entity/Concept/Overview 默认不会被模型整体覆盖。
10. Wiki 变更可 staging、校验、提交和回滚。
11. 服务重启不会永久丢失 queued/running 任务。
12. scheduled 重复来源可以正常记录为跳过，不会导致整次同步异常退出。
13. Qwen 在 Golden Corpus 上达到预先定义的成功率、证据覆盖率和截断率阈值。
14. 只有 Wiki 提交成功后才触发 Quartz publish；发布状态仍与 Ingest 状态分开。

## 9. 风险、缓解与回滚原则

| 风险 | 缓解措施 |
| --- | --- |
| 词法检索对同义词和隐含语义召回不足 | 先以可解释、可测试的词法基线建立指标；只有 Golden Corpus 证明不足时再引入 embedding 或混合检索 |
| token 估算与 Ollama 实际 tokenizer 有偏差 | 使用保守 fallback、独立 safety margin，并持续对比 `prompt_eval_count` 或等价 usage |
| Wiki 上下文预算过小造成证据遗漏 | 记录候选、分数和跳过原因，按 profile 在评测后调整 `wiki_context_max_tokens`，不取消硬上限 |
| 上下文预算过大增加 Ollama 延迟、显存或 OOM 风险 | 在 DGX 记录延迟、offload 和 OOM；优先减少低相关章节，不盲目提高 context/输出 |
| Markdown 分节破坏表格或代码围栏 | 使用结构感知切分和固定 fixture，候选无法安全切分时明确跳过或失败 |
| 检索器异常导致知识缺失 | 明确失败并保留诊断；禁止回退到完整 index/overview 或最近 5 篇 |

实现应拆成“专用 profile”“预算器”“检索器”“Ingest 接入/分块”“写入质量门”几个可独立审查和回滚的变更。短期代码回滚可以恢复上一稳定版本，但不得把无界全文上下文或静默模型 fallback 作为长期运行方案；配置、README 和部署命令必须始终与实际启用路径一致。
