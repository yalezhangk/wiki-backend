# Ingest 大文档分块抽取与分页生成计划

## 状态与用途

状态：Phase 0–4 已完成。2026-10-20 使用真实 49,655-token RM6 SeT Markdown、
`ollama_chat/qwen3.8:27b`、真实 MySQL/Wiki 完成 Ingest job 20，并由 Quartz publish job 16
完成修正后快照发布。

本计划用于为当前 `wiki-backend` Ingest 增加大文档处理路径，解决转换后 Markdown 超过
`SMALL_SOURCE_MAX_TOKENS=24_576` 时只能以 `ingest_source_context_too_large` 失败的问题。

本计划优先保证两件事：

1. 大文档能够在现有 Ingest API、任务和 Wiki 写入流程中成功入库。
2. 分块后仍能形成一份文档级一致的 Source 页面、Entity/Concept 页面和 patch，尽量避免
   章节遗漏、重复页面、参数丢失与跨块冲突。

服务重启恢复、分布式队列、人工审核、版本回滚、长期审计和完整来源追踪不是本计划的第一阶段
目标；这些能力可以在大文档路径稳定后继续演进。本计划也不修改 `llm-wiki-agent` 源码，不改变
Quartz 发布边界，不把浏览器上传的单个文档拆成多个独立 Ingest 任务。

## 当前问题

当前 `IngestService._ingest_source()` 只有一条主路径：

1. 读取或转换完整来源 Markdown。
2. 使用 `_assert_source_is_direct_ingestable()` 检查来源估算不超过 24,576 tokens。
3. 检索相关 Wiki 小节。
4. 把完整来源、Wiki 上下文和 Ingest 指令放入一个 Prompt。
5. 要求模型在一个 JSON 中返回完整 `IngestLLMResult`。
6. 一次性写入 Source、Entity、Concept、patch、index 和 log。

这条路径适合小文档，但大文档存在两个独立瓶颈：

- **输入瓶颈**：完整来源无法装入一次模型调用。当前本地
  `ollama_chat/qwen3.8:27b` 的 `max_input_tokens` 为 49,152；示例文件
  `ECATA1138-RM6-SeT中压开关柜-20220810.md` 约 49,655 个保守估算 tokens，正文自身已超过
  输入上限，尚未包含固定指令和 Wiki 上下文。
- **输出瓶颈**：即使输入能够放入模型，当前 JSON 同时携带完整 Source 页面、多个新知识页面、
  多个 patch、索引条目和 log，容易超过 8,192 tokens 的输出预算并产生截断 JSON。

因此，不能通过单纯提高 `SMALL_SOURCE_MAX_TOKENS` 解决；也不能把来源切成多个文件分别调用当前
Ingest。后者会把一份来源拆成多个 Source 页面，并导致重复 Entity/Concept、相互竞争的 patch
和碎片化索引。

## 成功标准

实现完成后应满足：

1. 不超过 24,576 tokens 的来源继续使用现有单次完整来源路径，行为和结果不回归。
2. 超过该阈值的来源自动进入大文档路径，不再因直通来源上限直接失败。
3. 一个上传文件始终只对应一个 Ingest job、一个 Source 页面和一个 Sources 索引条目。
4. 所有来源 chunk 都必须完成抽取并参与文档级归并；任何 chunk 永久失败时不得写入部分 Wiki。
5. Chunk 阶段只产出来源证据，不直接创建或修改 Wiki 页面。
6. 最终页面分开生成，单个页面的输出预算有界，不要求模型一次返回全部 Markdown 页面。
7. 最终结果仍转换为当前 `IngestLLMResult`，复用现有 Pydantic 校验、路径白名单、索引校验、
   `base_hash` 检查、Wiki 写入、Ingest 校验和 Publish 入队逻辑。
8. 示例 RM6 SeT 文档能够完整覆盖其主要章节和关键规格，并在真实 Qwen Ingest 验证中成功完成。

## 非目标

- 第一阶段不实现跨进程 checkpoint 或服务重启续跑。
- 第一阶段不引入 Celery、Redis、Kafka、向量数据库或新的工作流引擎。
- 不允许 chunk 独立写 Wiki，也不提供“部分成功后先发布”的模式。
- 不把长文分块参数开放给浏览器请求；参数先由服务端代码控制。
- 不为了大文档路径修改 Quartz 构建、发布 debounce 或 `quartz/public` 切换机制。
- 不以“摘要的摘要”替代关键事实、技术规格和表格证据。
- 不在本计划中修改 `wiki/overview.md`。

## 总体设计

保留现有小文档快速路径，并增加大文档三阶段路径：

```text
转换后的 Markdown
        |
        +-- <= 24,576 tokens --> 现有单次 Ingest 路径
        |
        +-- > 24,576 tokens
                |
                v
        结构感知分块
                |
                v
        Chunk Evidence Extraction
                |
                v
        Document Knowledge Plan
                |
                v
        Source / Entity / Concept / Patch 分别生成
                |
                v
        组装 IngestLLMResult
                |
                v
        复用现有写入、校验和发布入队
```

大文档路径仍在一个 `_ingest_source()` 任务内完成。第一阶段继续使用现有粗粒度
`stage="extracting"`，通过 `progress_percent` 和结构化日志报告 chunk、reduce 和页面生成进度，
避免仅为展示进度扩展 API 状态枚举。后续如 UI 确实需要再增加细分 stage。

## 设计决定

### 1. 小文档路径保持不变

`SMALL_SOURCE_MAX_TOKENS=24_576` 继续作为直通路径阈值，而不是整个 Ingest 产品的文档上限。

- 小文档继续完整进入现有 Prompt。
- 大文档不调用 `_assert_source_is_direct_ingestable()` 失败，而是切换到新路径。
- 最终每次 LLM 调用仍必须通过模型档案的 `PromptBudget` 检查。
- 不通过截断原始来源来伪装成功。

### 2. Chunk 只是内部证据单元

Chunk 不拥有独立文件名、Source slug、Ingest job 或 Wiki 写入权限。每个 chunk 至少包含：

- 稳定的 `chunk_id`；
- 原始行号范围；
- 章节 breadcrumb；
- 完整 chunk 文本；
- 保守 token 估算。

Chunk ID 应由顺序号和内容 hash 构成或派生，保证同一输入在相同分块算法下结果稳定。

### 3. 来源抽取与 Wiki 集成分离

Chunk Extraction Prompt 不注入现有 Wiki 内容，只抽取来源自身的事实、实体、概念、规格和内部
冲突。这样可以：

- 减少每个 chunk 的输入占用；
- 避免模型把已有 Wiki 事实误写成新来源事实；
- 让每个 chunk 的输出能够独立校验和重试。

所有 chunk 抽取完成并归并后，再根据归并结果检索 Wiki，并规划新页面与已有页面 patch。

### 4. 不让最终模型一次返回全部页面

文档级 Reduce 只返回轻量的 `IngestKnowledgePlan`，不返回所有页面的完整 Markdown。随后根据
计划逐页生成 Source、Entity、Concept 和 patch。每次页面调用只携带该页面需要的证据和已有
Wiki 上下文。

最终由后端组装当前 `IngestLLMResult`，保持写入层职责不变。

### 5. 第一版优先顺序执行模型调用

本地 Qwen 首版按 chunk 和页面顺序调用，不并发轰击 Ollama。先保证稳定性和质量，再根据 DGX
显存、吞吐和模型服务实际行为决定是否增加小并发。普通纯 Python 分块和校验不受此限制。

## 结构化分块方案

### Block 解析

先把 Markdown 转换为顺序 block，第一版支持：

- ATX Markdown 标题 `#` 到 `######`；
- 连续普通段落；
- 连续列表；
- Markdown 表格；
- fenced code block；
- 空行边界；
- 无规范标题文档中的目录章节名称。

表格和 fenced code block 必须作为不可在行中间切断的 block。单个 block 超过 chunk 硬上限时，
只对普通段落进行二级切分；超大表格应单独形成 chunk，并在 Prompt 中标记其内容类型。

### 章节识别

优先使用规范 Markdown 标题。对于示例文档这类 PDF 转换文本，增加受控的目录辅助规则：

1. 从文档前部识别短行目录条目和可能的页码后缀。
2. 只把在后续正文中再次独立出现的目录名称视为章节边界。
3. 目录识别不成功时退化为段落/block 累积分块，不调用 LLM 决定边界。

目录辅助规则必须保守，不能把普通句子中的短语误判为大量章节。

### Chunk 大小

第一版使用服务端常量：

| 参数 | 建议值 | 说明 |
| --- | ---: | --- |
| `LARGE_SOURCE_CHUNK_TARGET_TOKENS` | 10,000 | 达到目标后在下一个安全 block 边界切分 |
| `LARGE_SOURCE_CHUNK_MAX_TOKENS` | 16,000 | 单个 chunk 的硬上限 |
| `LARGE_SOURCE_CHUNK_OVERLAP_TOKENS` | 384 | 仅用于普通连续文本的边界上下文 |

重叠内容需要标记，Reduce 去重时不得把它当成两份独立证据。章节标题和 breadcrumb 应作为元数据
重复提供，不计为来源正文丢失或重复。

分块器必须满足内容覆盖检查：除显式标记的 overlap 外，所有原始 block 必须恰好属于一个主 chunk，
不得静默遗漏或重排。

## 阶段一：Chunk Evidence Extraction

新增大文档 chunk 专用 Prompt，例如 `app/prompts/ingest_chunk.md`。其职责仅为来源抽取，不得规划
Wiki 路径、生成完整 Wiki 页面或修改已有内容。

建议新增 Pydantic 模型：

```python
class IngestChunkEvidence(BaseModel):
    evidence_id: str
    statement: str
    evidence_text: str
    source_lines: str
    category: Literal[
        "claim",
        "specification",
        "definition",
        "requirement",
        "relationship",
        "warning",
    ]


class IngestChunkExtraction(BaseModel):
    chunk_id: str
    section_title: str
    summary: str
    evidence: list[IngestChunkEvidence]
    entities: list[str]
    concepts: list[str]
    internal_conflicts: list[str]
```

具体字段可在实现时按现有 Pydantic 风格微调，但必须保留以下质量约束：

- `chunk_id` 必须与请求中的 chunk 一致。
- 关键数字、单位、型号、适用条件和否定条件必须进入 `evidence`，不能只留在 summary。
- `evidence_text` 必须是短的来源原文证据，不允许模型生成大段来源副本。
- `source_lines` 必须落在该 chunk 行号范围内。
- 没有事实价值的页眉、页脚、目录重复和营销噪声可以省略，但 chunk 仍需返回成功的空/低信息结果。
- Chunk 不能因为“没有值得创建页面的内容”而被视为失败。

每个 chunk 输出先执行 JSON 解析和 Pydantic 校验。无效 JSON 可以使用专用 repair Prompt 修复一次；
repair 只接收无效响应和 chunk Schema，不重新发送原始大文档。模型明确报告不可处理或修复仍失败时，
整个 Ingest 失败且不得写 Wiki。

## 阶段二：Document Knowledge Plan

所有 chunk extraction 完成后执行文档级 Reduce。Reduce 输入只包含：

- 文档文件名及必要元数据；
- 所有 chunk 的章节、摘要和结构化 evidence；
- 每个 chunk ID；
- 当前日期和知识规划指令。

建议新增轻量 Pydantic 模型：

```python
class IngestPlannedPage(BaseModel):
    page_type: Literal["source", "entity", "concept"]
    title: str
    path: str
    purpose: str
    supporting_chunk_ids: list[str]
    supporting_evidence_ids: list[str]


class IngestPatchIntent(BaseModel):
    page_type: Literal["entity", "concept"]
    existing_path: str
    purpose: str
    supporting_chunk_ids: list[str]
    supporting_evidence_ids: list[str]


class IngestKnowledgePlan(BaseModel):
    title: str
    slug: str
    document_summary: str
    key_claims: list[str]
    covered_chunk_ids: list[str]
    planned_pages: list[IngestPlannedPage]
    patch_intents: list[IngestPatchIntent]
    contradictions: list[str]
```

Reduce 必须完成：

1. 合并跨 chunk 的重复事实、同义实体和同义概念。
2. 保留数字、单位、型号及其适用条件，不用模糊摘要替代。
3. 确定整个文档唯一的 title、slug 和 Source 页面规划。
4. 只为具有跨来源复用价值的关键对象规划 Entity/Concept 页面，避免为每个目录标题建页。
5. 报告文档内部明确冲突，但不凭猜测消解冲突。
6. 在 `covered_chunk_ids` 中包含全部 chunk；缺失、重复或未知 ID 均视为 Schema 后验校验失败。
7. 每个计划页面至少引用一个存在的 chunk 和 evidence。

如果所有 chunk extraction 渲染后仍超过 Reduce 的输入预算，使用固定 fan-in 的层级归并：先按原始
章节顺序将相邻 extraction 分组生成 `SectionSynthesis`，再基于这些结构化 SectionSynthesis 生成
最终计划。禁止直接截断末尾 chunk，禁止仅保留前 N 个章节。

对于示例约 49,655 tokens 的文档，预计 7 至 8 个 chunk 的结构化 extraction 可以在一次 Reduce
中完成；只有真实预算检查不通过时才启用层级归并。

## Wiki 检索与集成规划

当前 `WikiContextRetriever` 不再用完整大文档正文构造检索词，而是使用 Knowledge Plan 中的：

- title；
- key claims；
- 规范化实体和概念名称；
- 计划页面的 purpose。

在既有 `wiki_lock` 内拍摄当前 Wiki 快照，并检索相关 Source、Entity、Concept 和 Overview 小节。
随后执行一次轻量的 Wiki integration planning，完成：

- 计划中的 Entity/Concept 是新建页面还是已有页面 patch；
- 已有页面的准确路径和 snapshot hash；
- 来源与现有 Wiki 之间有证据支持的 contradictions；
- 每个页面生成调用需要的 Wiki 小节。

模型只能 patch 实际检索到且带 snapshot hash 的页面。未命中的已有页面不得由模型猜测路径。

实现时可以把 Wiki integration planning 合并进 Document Knowledge Plan 的后半段，也可以作为一次
独立调用；选择标准是最终 Prompt 是否稳定满足预算。不得为了减少一次调用而把完整 Wiki 上下文
重新注入所有 chunk extraction Prompt。

## 阶段三：按页面生成

### Source 页面

单独生成一个完整 Source 页面。输入包括：

- 文档 Knowledge Plan；
- 所有与 Source 页面章节相关的 extraction/evidence；
- 允许链接的已检索 Wiki 页面列表；
- 当前 Source 页面格式契约。

Source 页面应覆盖文档的主要章节，而不是只返回一个泛化摘要。对于产品手册，应优先保留：

- 产品范围和用途；
- 关键特性；
- 型号、额定值、尺寸、环境条件等主要技术规格；
- 功能与模块；
- 数字化能力；
- 安装和连接要求；
- 注意事项、限制和适用条件。

关键规格应从对应 evidence 重新提供给页面生成 Prompt，避免只依据多层自然语言摘要恢复数字。

### 新 Entity/Concept 页面

每个计划页面单独生成，输入只包含：

- 页面计划；
- 该页面引用的 chunk/evidence；
- 必要的相关 Wiki 片段；
- 新页面路径和格式约束。

页面返回后封装成现有 `IngestGeneratedPage`。后端根据 path、title 和计划中的单行摘要构造或验证
对应 `IngestIndexEntry`，避免模型产生路径不一致的索引条目。

### 已有页面 patch

每个 patch intent 单独生成 `IngestPagePatch`，调用只接收：

- 目标页面被检索到的小节；
- 目标 snapshot hash；
- 与 patch 直接相关的来源 evidence；
- 当前 `append_section` / `replace_section` 限制。

现有 `base_hash`、受保护章节和允许路径校验继续生效。没有足够证据时允许返回“不生成 patch”，
不能为了满足计划而强行修改已有页。

### 最终组装

页面生成全部成功后，后端组装当前 `IngestLLMResult`：

- `title`、`slug`、`contradictions` 来自最终 Knowledge Plan；
- `source_page` 来自 Source 页面生成结果；
- `entity_pages`、`concept_pages` 来自逐页生成结果；
- `entity_patches`、`concept_patches` 来自 patch 生成结果；
- index entry 使用已验证路径和计划摘要生成；
- `log_entry` 使用 title 和 key claims 生成或通过一个很小的模板生成。

组装后必须再次通过 `IngestLLMResult.model_validate()`，然后进入现有
`_write_ingest_result()`。在此之前不写任何正式 Wiki 文件。

## 建议的代码边界

为避免继续扩大已经较长的 `ingest_service.py`，建议新增一个聚焦模块，例如：

- `app/services/ingest_large_document.py`
  - block/chunk 数据类；
  - 确定性分块器；
  - chunk coverage 校验；
  - extraction/plan/page 结果的组装辅助逻辑。
- `app/prompts/ingest_chunk.md`
- `app/prompts/ingest_reduce.md`
- `app/prompts/ingest_source_page.md`
- `app/prompts/ingest_knowledge_page.md`
- `app/prompts/ingest_page_patch.md`

Pydantic 边界模型放在 `app/schemas/ingest.py` 或单独的 Ingest 内部 schema 模块，具体选择以避免 API
响应模型和内部 LLM 模型混杂为准。不要为单次使用逻辑建立通用工作流框架。

`IngestService` 只负责：

1. 判断走直通路径还是大文档路径；
2. 解析 Ingest 模型档案；
3. 调用大文档编排器；
4. 接收最终 `IngestLLMResult`；
5. 复用现有写入、校验、任务完成和 Publish 入队。

所有新 Python 函数保持完整类型标注；边界数据使用 Pydantic；日志通过 `logging` 输出，不记录完整
来源、完整 evidence 或敏感模型响应。

## 模型预算建议

第一版使用以下目标而不是把模型窗口全部填满：

| 调用类型 | 目标输入 | 建议最大输出 |
| --- | ---: | ---: |
| Chunk extraction | 8k–16k | 2k–4k |
| Document reduce | 10k–25k | 3k–5k |
| Source page | 10k–25k | 4k–8k |
| 单个 Entity/Concept | 3k–12k | 2k–4k |
| 单个 patch | 3k–10k | 1k–3k |

实际调用仍受 `IngestModelProfile.capabilities` 和 `WIKI_BACKEND_INGEST_LLM_MAX_TOKENS` 控制。
如不同阶段需要更小输出上限，应由后端在调用时传入阶段上限，并且不得超过模型档案或全局 Ingest
输出上限。

## 失败处理边界

本计划暂不实现跨重启恢复，但一次任务内必须有清晰的失败行为：

- Chunk 调用遇到现有定义的瞬态错误时，复用当前一次重试策略。
- JSON 无法解析时允许使用对应 Schema 的 repair 调用一次。
- 单个 chunk、Reduce 或页面生成最终失败时，整个 job 失败。
- 失败前不得写正式 Wiki，因此不需要跨多个已写页面回滚。
- 页面生成响应截断时只重试该页面，不重做已经验证通过的 chunk extraction。
- 日志记录 `job_id`、阶段、chunk/page 标识、估算预算和错误类型，不记录完整正文。
- 第一阶段可沿用当前失败上传文件清理语义；保留失败原件和断点恢复另立计划。

## 分阶段实施步骤

### Phase 0：测试契约与路由

1. 增加“大于 24,576 tokens 进入大文档路径”的服务测试。
2. 保留“小于等于阈值仍进入现有直通路径”的回归测试。
3. 为测试注入 fake large-document orchestrator 或 fake LLM 调用，不连接真实模型、不写真实 Wiki。
4. 明确大文档路径最终返回 `IngestLLMResult`，不改变 `_write_ingest_result()` 输入契约。

验收：路由边界测试通过；现有 `test_source_at_small_source_limit_is_accepted_for_direct_path` 和 Prompt
预算测试不回归。

### Phase 1：确定性分块与 Chunk Extraction

1. 实现 Markdown block 解析和结构感知分块。
2. 实现 chunk coverage、顺序、大小和 overlap 校验。
3. 新增 `IngestChunkExtraction` Schema 和 Prompt。
4. 顺序调用模型，逐块解析、校验并记录进度。
5. 任意 chunk 失败时停止后续编排且不写 Wiki。

验收：规范 Markdown、无标题 Markdown、表格、超大章节和示例文件的分块测试通过；所有原始内容均
被覆盖，除显式 overlap 外不重复。

### Phase 2：Document Reduce 与 Wiki Integration Plan

1. 新增 `IngestKnowledgePlan` 及后验覆盖校验。
2. 实现 extraction 去重、全 chunk 覆盖和文档级规划 Prompt。
3. 使用计划中的关键实体、概念和 claims 调用现有 `WikiContextRetriever`。
4. 将新页面与已有页面 patch 意图解析清楚，并绑定实际 snapshot hash。
5. 当 Reduce 输入超限时启用固定 fan-in 的层级归并。

验收：相同实体不会因不同 chunk 创建重复计划页；计划覆盖每个 chunk；无来源证据的页面或 patch
无法通过后验校验。

### Phase 3：按页面生成并组装现有结果

1. 单独生成 Source 页面并校验 Frontmatter、title 和 slug。
2. 根据计划逐个生成新 Entity/Concept 页面。
3. 根据实际检索结果逐个生成受控 patch。
4. 生成或确定性构造 index entry 与 log entry。
5. 组装并校验 `IngestLLMResult`。
6. 复用 `_write_ingest_result()`、`_validate_ingest()` 和 Publish 入队。

验收：单个页面输出不会因其他页面数量增加而截断；全部生成完成前 Wiki 保持不变；最终写入后的
路径、索引和链接校验与小文档路径一致。

### Phase 4：真实模型质量验证与调优

1. 在隔离的 agent/Wiki 测试目录中使用当前 Qwen 完整执行示例文档。
2. 根据真实结果调整 chunk 目标值、Prompt 和 evidence 字段；不先增加并发。
3. 隔离验证通过后，明确真实 Wiki、MySQL 和发布副作用，再通过页面执行一次真实 Ingest。
4. 确认 Ingest `succeeded` 只表示 Wiki 写入成功；必须等待 `publication.status=published` 才能宣称
   Quartz 站点已更新。

验收：达到下文“示例文档质量验收”要求，并完成默认完整单元测试和 DGX 健康检查。

完成记录（2026-10-20）：

- 隔离运行完成 5 个确定性 chunk、Document Reduce、Wiki 检索和 5 个分页调用，
  且未修改 Wiki。
- 真实 API job 20 成功创建 1 个 Source、2 个 Entity、2 个 Concept，更新
  `index.md` 和 `log.md`，MySQL 终态为 `succeeded/completed/100`。
- 真实输出暴露的 chunk 与 Reduce 过长 JSON 问题已通过 Prompt 与 Pydantic 双重上限修复；
  Source Frontmatter 结构和路径式 Wikilink 也已增加后验校验。
- Quartz publish job 16 终态为 `succeeded`，`quartz/public` 已切换到 release 16，
  Nginx `:8080` 可访问 Source 和新知识页。
- 最终完整回归为 318 个测试通过、9 个跳过。

## 自动化验证步骤

### 分块器单元测试

- 空内容和低质量内容仍由现有质量检查拒绝，不进入分块器。
- 24,576 tokens 走直通路径，24,577 tokens 走大文档路径。
- 标题边界优先于目标 token 边界。
- Markdown 表格不会在数据行中间切开。
- fenced code block 不会被切开。
- 超大普通段落能够在硬上限内二级切分。
- 所有主 block 按原顺序覆盖，不能遗漏或无意重复。
- 同一输入多次分块得到相同 chunk ID、顺序和边界。

### 编排服务单元测试

- 每个 chunk 恰好调用一次 extraction；瞬态错误最多按现有策略重试一次。
- 一个 chunk 最终失败时 Reduce、页面生成和 Wiki 写入都不会发生。
- Reduce 的 `covered_chunk_ids` 缺失、重复或包含未知 ID 时失败。
- 页面计划引用未知 evidence ID 时失败。
- 多个 chunk 中的同名实体只生成一个计划页面。
- Wiki 检索使用 Knowledge Plan，而不是重新注入完整大文档。
- 页面生成只接收该页面声明的 chunk/evidence。
- 一个页面响应截断不会要求重新抽取所有 chunk。
- 最终组装结果通过 `IngestLLMResult`、路径、索引和 patch hash 校验。
- 最终结果校验完成前，不调用 `_write_ingest_result()`。

### 现有流程回归测试

- 小 Markdown、PDF 原生转换、PDF OCR fallback 和其他支持格式保持现有行为。
- manual/scheduled 的 `source_file` / `source_url` 修正保持现有行为。
- 失败任务继续释放 `document_name_key`。
- Source、Entity、Concept、index 和 log 写入结果保持原有路径约束。
- Publish 仍只在 Ingest Wiki 写入成功后入队。
- 现有完整测试通过：

  ```bash
  .venv/bin/python -m unittest discover -s tests
  ```

## 示例文档质量验收

示例文件不提交到仓库；自动化测试使用等价的合成 fixture，真实文件只用于显式 DGX 验证。

对 `ECATA1138-RM6-SeT中压开关柜-20220810.md` 的验收至少包括：

1. 不再出现 `ingest_source_context_too_large`。
2. 只生成一个 Source 页面和一个 Sources 索引条目。
3. Source 页面覆盖以下主要章节：
   - 概述；
   - 产品系列说明；
   - 功能/模块说明；
   - 核心元件和附件；
   - 数字化解决方案；
   - 安装和连接。
4. 关键事实没有因 Reduce 丢失，至少抽取并正确表达原文中的：
   - `24kV及以下` 产品范围；
   - 主回路 `40年以上` 使用寿命；
   - `3mm 304不锈钢` 气箱；
   - 最大 `6单元共箱`；
   - 主要数字化能力和安装连接要求。
5. 数字、单位、产品型号和适用条件能够在对应 chunk evidence 中找到支持，不出现来源中不存在的
   额定值或型号。
6. `RM6 SeT` 等核心实体不会因章节拆分创建多个重复页面。
7. 最终 JSON、Source 页面或任一知识页面均不发生输出截断。
8. 现有 Wiki 中存在相关页面时只生成合法 patch；不存在时才生成新页面。
9. Ingest job 达到 `succeeded` 后检查 `created_pages`、`updated_pages`、`contradictions` 和
   `validation`；只有对应发布任务成功后再检查 Quartz 页面。

真实验证顺序：

```bash
cd /home/dgx/Projects/knowledge_base_mkt/wiki-backend
.venv/bin/python -m unittest discover -s tests
curl --fail --silent --show-error http://127.0.0.1:8081/api/health
curl --fail --silent --show-error http://127.0.0.1:8080/api/health
```

涉及真实 MySQL、Wiki、Ollama 和 Quartz 发布的最后一次上传验证必须先明确环境和副作用，不在普通
单元测试中自动执行。

## 文档同步要求

每个 Phase 落地时同步更新：

- `docs/ingest-flow.md`：只描述已经实现的实际流程；
- README 中的 Ingest 能力和限制；
- 如新增配置，再同步 `.env.example` 和 `app/config.py`；
- API schema 文档仅在对外字段或 stage 发生变化时更新。

大文档能力已通过 DGX Qwen 端到端验证；后续更改 chunk、Reduce、分页生成或写入合约时，
仍必须同时运行 fake LLM 回归和显式真实模型验证。

## 明确不采用的方案

- 直接把 `SMALL_SOURCE_MAX_TOKENS` 提高到模型最大输入。
- 把一个大文档拆成多个浏览器上传或多个 Source 页面。
- 每个 chunk 独立运行当前 `ingest.md` 并直接写 Wiki。
- 只生成 chunk 摘要，再对摘要做一次泛化总结。
- 在一个最终 JSON 中同时返回所有完整页面。
- 为解决当前问题提前引入分布式任务平台、向量数据库或复杂恢复协议。

本计划的核心取舍是：只增加保证大文档输入与输出有界所必需的三阶段编排，同时保留当前已经存在
的写入、校验和发布机制。
