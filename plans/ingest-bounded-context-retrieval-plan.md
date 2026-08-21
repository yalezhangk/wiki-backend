# Ingest 有界来源与确定性 Wiki 上下文检索计划

## 状态与范围

状态：待实施。本计划只定义逐步收紧 Ingest Prompt 上下文的方案，不在本次计划中
修改代码、配置或真实 Wiki 数据。

当前实现会将完整 `wiki/index.md`、完整 `wiki/overview.md` 和按 mtime 倒序的五篇
完整 Source 页面拼入一次 LLM 调用；该策略与新来源的主题无关，且随 Wiki 增长而无上限。
见 `app/services/ingest_service.py` 的 `_build_wiki_context()`。

本计划的目标是：

1. 小来源保持完整，不因给旧 Wiki 腾位置而截断。
2. Wiki 上下文仅包含与新来源相关、可复现且受预算约束的证据片段。
3. 每一次模型调用在最终渲染后都满足服务端模型档案的 token 包络。
4. 先替换上下文组织；长文 chunk/reduce、写入质量门和重试恢复各自单独演进，避免大改。

非目标：

- 不改变上传 10 MiB 文件大小限制。
- 不让 API 客户端指定模型、预算或检索结果。
- 不在第一阶段引入向量数据库、Embedding 服务或 LLM 驱动的页选择。
- 不修改 `llm-wiki-agent` 源码。

## 已确认的模型运行预算

DeepSeek 官方能力为 1,000,000 token 上下文、最大 384,000 token 输出；这里采用较小的
服务端运行上限，以控制成本、延迟、无关上下文干扰和结构化 JSON 截断范围。

| Ingest 模型 | context_window | max_input | max_output | safety_margin |
| --- | ---: | ---: | ---: | ---: |
| `deepseek/deepseek-v4-pro` | 1,000,000 | 131,072 | 16,384 | 16,384 |
| `deepseek/deepseek-v4-flash` | 1,000,000 | 98,304 | 8,192 | 8,192 |
| `ollama_chat/qwen3.6:35b` | 65,536 | 49,152 | 8,192 | 8,192 |

所有模型均应满足：

```text
final_input_tokens <= max_input_tokens
final_input_tokens + max_output_tokens + safety_margin_tokens <= context_window_tokens
```

`final_input_tokens` 必须在 `render_prompt()` 后重新估算；固定模板、消息封装、来源、Wiki
片段均需计入。计数器在没有厂商 tokenizer 时使用保守估算，并保留安全余量；不得把估算值
伪装为精确 tokenizer 结果。

## 小来源的明确判定

第一阶段统一定义：**转换为 Markdown 后，来源内容的保守估算不超过 24,576 tokens，才是小来源。**

这个数字不是原始文件字节数，也不是字符数；它以转换后的 Markdown 为准。选择 24,576 的原因是
它可在 Qwen 的 49,152 最大输入内为固定指令、检索到的 Wiki 证据和消息封装留出空间，因而可在
三种受支持模型间使用同一个可预测的直通规则。

小来源的处理契约：

1. 完整 Markdown 必须进入 Prompt，禁止截断来源正文。
2. 在来源、固定指令和输出/安全预留扣除后，Wiki 只能使用剩余预算，且受
   `WIKI_CONTEXT_MAX_TOKENS=16,384` 的上限约束。
3. 若最终 Prompt 仍不满足当前模型包络，先减少或清空 Wiki 上下文；若仍不满足，则在调用前
   失败，错误类别为 `ingest_source_context_too_large`，不得截断来源后标记成功。

大来源的处理契约：

- 超过 24,576 tokens 不再走“单次完整来源”路径；第一阶段明确失败并给出稳定错误。
- 后续独立计划再引入按 Markdown 结构切块、事实抽取和 reduce；该阶段才会使大来源可入库。
- 不依据 10 MiB 上传大小决定是否切块：一个很小的 PDF 也可能转换出很长文本，反之亦然。

## 确定性相关检索：第一阶段实现

### 1. Wiki 快照

新增只读 `WikiSnapshot`：在既有 `wiki_lock` 内收集允许参与检索的路径、UTF-8 内容与 SHA-256。
快照至少覆盖：

- `wiki/index.md`（仅目录/元数据用途）；
- `wiki/overview.md`；
- `wiki/sources/*.md`；
- `wiki/entities/*.md`；
- `wiki/concepts/*.md`。

明确排除 `wiki/log.md`、`wiki/syntheses/`、maintenance/health/lint report、graph 产物及任何
非知识页面。检索后到写入前，写入层应检查快照 hash；发生变化时不依据过期证据覆盖已有页。
该写入前检查可在检索阶段稳定后单独接入，避免第一阶段扩大改动面。

### 2. 候选与查询词

新增纯本地 `WikiContextRetriever`，不调用 LLM：

1. 从来源的标题、Markdown headings、ASCII 词、中文连续词和已有 WikiLink 生成规范化查询词；
   停用词、过短词和重复词删除。
2. 将 Overview 与每个知识页按 Markdown heading 切成小节；每个候选带
   `path`、`heading`、`page_type`、`content`、`snapshot_hash`。
3. 使用确定性词法评分：标题命中权重高于正文命中，来源内的显式 WikiLink 权重最高；同分按
   `path`、`heading` 的字典序排序。
4. `index.md` 不再作为完整上下文，只可提供候选路径/标题元数据；不得回退为“最近五篇”。

候选生成和排序必须可离线单测：同一个来源与同一个快照必定得到相同的候选顺序。

### 3. 预算内选择与渲染

候选不会整页无界注入。选择器按以下规则填充剩余预算：

1. 先计算 `available_wiki_tokens`：模型 `max_input` 减固定 Prompt、完整小来源、消息封装；
   再与 16,384 取较小值。
2. 以“标题/Frontmatter + 完整命中小节”为最小证据单元。单节超过每节上限时，不截断到任意
   字符位置；优先选择更小的同页命中小节，无法容纳则跳过并记录原因。
3. 按分数顺序选择；优先保证至少一个相关 Overview 小节，随后在 Entity、Concept、Source
   之间保持类型多样性。只有实际命中才会选择，零命中时渲染显式空上下文占位，绝不使用
   “最近五篇”回退。
4. 每个渲染片段都标注 `path`、`heading` 和快照 hash；Prompt 明确规定模型只能把已给出的
   既有页面视为可更新/可链接的已有事实。
5. 将选中片段加入 Prompt 后再次计算总输入；不满足硬预算时，按最低分候选逐个移除并重算。

日志只允许记录模型档案、预算分项、候选路径、分数、选择/跳过原因和 hash 前缀；不得记录
来源正文、Wiki 正文、密钥或完整模型响应。

## 分阶段实施与验收

### Phase 0：契约和测试先行

- 为 DeepSeek Pro/Flash 写入已确认的服务端能力与独立输出预算；Qwen 保持既有包络。
- 新增 `TokenEstimator`、`PromptBudget` 的纯单元测试，不改现有写入流程。
- 覆盖小来源边界：24,576 tokens 接受完整正文，24,577 tokens 不可进入直通路径。
- 覆盖最终渲染后超限：不得调用 LLM、不得写 Wiki。

验收：模型能力测试和 Ingest 服务单测通过；现有 Qwen 超窗测试继续通过。

### Phase 1：替换固定 Wiki 上下文

- 新增 `WikiSnapshot` / `WikiContextRetriever` 及其单元测试。
- 仅替换 `IngestService._build_wiki_context()` 的调用路径，保留当前单次小来源主调用、JSON 解析和
  Wiki 写入流程。
- 删除“完整 index + 完整 overview + 最近五 Source”的上下文拼装逻辑。
- 为检索失败、零候选和预算为零定义显式行为；任何一种都不得回退到无界完整 Wiki。

验收：

- 无关但最近修改的 Source 不进入 Prompt；相关 Entity/Concept 可以进入。
- Wiki 扩大后小来源最终 Prompt 仍在预算内。
- 同一输入/快照的候选与渲染结果逐字一致。
- Prompt 仅包含被选中的已有路径，且所有路径都来自快照。

### Phase 2：JSON repair 上下文收缩

- repair 调用只传 JSON 合约和无效响应，不重传来源与 Wiki。
- 保留“只修 JSON 结构、不增加事实”的约束与 Pydantic 后验校验。

验收：repair Prompt 不含 `=== SOURCE START ===`、原 Wiki 片段或主 Prompt；无效 JSON 仍可被
修复时保持原有成功行为。

### Phase 3：大来源 chunk/reduce（单独批准后实施）

- 只处理超过 24,576 tokens 的转换后 Markdown。
- 按标题、段落、代码围栏和 PDF 页界切块；逐块抽取带来源位置的事实草稿，再固定 fan-in reduce。
- 最终写入仍使用 Phase 1 的相关 Wiki 检索和硬预算；任意阶段不能静默丢失来源内容。

验收：大来源不会因“单个 Prompt 过大”直接进入无界调用；每一条最终事实都可追踪到来源块。

## 当前不应顺手修改的部分

- 上传接口、MySQL 队列语义、Quartz 发布流程。
- Source origin/frontmatter 修正逻辑。
- Entity/Concept 的写入策略与总体质量门；只在检索快照稳定后另行收紧“可更新页面”契约。
- `llm-wiki-agent`、Quartz 或生成的 `public/` 文件。

## 文档同步要求

代码实施每个 Phase 时同步更新 `docs/ingest-flow.md`，仅描述已落地行为；未实施的 Phase 必须保持在
本计划中，不能提前写入“当前工作流”。
