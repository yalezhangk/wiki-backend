# Ingest Prompt 上下文预算与相关页面检索修复计划

## 1. 计划状态

- 状态：待实施（2026-08-17 建立）
- 范围：`wiki-backend` Ingest Prompt 的 Wiki 上下文构建
- 目标问题：
  1. `wiki/index.md`、`wiki/overview.md` 全文进入 Prompt，随知识库增长而无上限膨胀。
  2. 当前只取最近修改的 5 篇 Source，不能保证选中与新文档真正相关的历史页面。
- 本计划只描述修复方案，不在本文件中实施业务代码。

## 2. 当前代码事实

当前 `IngestService._build_prompt()` 调用 `_build_wiki_context()`，后者执行：

1. 读取完整 `wiki/index.md`。
2. 读取完整 `wiki/overview.md`。
3. 按文件修改时间倒序读取最近 5 个 `wiki/sources/*.md`，并把全文加入 Prompt。
4. 再把完整的 `app/prompts/agent_instructions.md`、当前源文档全文和输出契约拼入同一个 Prompt。

当前行为没有：

- 输入 token 预算。
- 为模型输出预留上下文。
- 对 `index.md`、`overview.md` 或选中页面的独立预算。
- 基于新文档内容的相关性检索。
- 对最终 Prompt 大小的硬性校验。
- 记录本次实际选择了哪些 Wiki 页面或章节的结构化诊断信息。

因此，知识库越大，Prompt 越容易发生上下文溢出、重要内容被模型忽略、响应截断和结果准确率下降。即使 Prompt 尚未超出模型硬限制，“最近 5 篇”也可能遗漏真正相关的旧 Source、Entity 或 Concept。

## 3. 修复目标

### 3.1 Prompt 大小必须有硬上限

每次调用 LLM 前必须能够计算并验证：

```text
固定指令预算
+ 当前源文档预算
+ Wiki 相关上下文预算
+ Prompt 包装开销
+ 输出预留
+ 安全余量
<= 当前 Ingest 模型的上下文窗口
```

不得依赖模型在超限后自行截断，也不得静默截断源文档或 Markdown 结构。

### 3.2 Wiki 上下文必须按相关性选择

用“相关页面和相关章节”替代“最近 5 篇 Source 全文”。检索范围至少包含：

- `wiki/sources/*.md`
- `wiki/entities/*.md`
- `wiki/concepts/*.md`
- `wiki/overview.md` 的分节内容

`wiki/index.md` 只作为目录和检索元数据来源，不再默认把全文发给 LLM。

### 3.3 方案必须兼容当前 DeepSeek 和后续 Ollama

Prompt 预算和检索逻辑不能绑定单一 provider。计划实施后应支持：

- 当前 `deepseek/deepseek-v4-pro` Ingest。
- 后续 `ollama_chat/qwen3.6:35b` Ingest。
- 不同模型配置不同上下文窗口和输出预留。

实际 Ollama `num_ctx` 必须在 DGX 上核实；不能仅根据模型名称假设上下文大小。

### 3.4 选择过程必须可复现、可测试

相同 Wiki 内容和相同源文档应得到稳定的候选排序。文件修改时间只能作为同分项的最终排序条件，不能继续充当主要相关性指标。

## 4. 非目标

以下事项与本问题有关，但不纳入本计划的首轮实施：

- 不在本计划中重构完整的长文档分块提取流水线。
- 不引入向量数据库或外部 embedding 服务。
- 不修改 Entity、Concept、Overview 的写入合并策略。
- 不重构 MySQL Ingest 队列。
- 不改变 Quartz 发布流程。
- 不修改 `llm-wiki-agent` 源码。

如果源文档本身超过可用输入预算，首轮实现应在调用 LLM 前返回明确、稳定的“源文档超出上下文预算”错误；后续由独立的长文档分块计划解决，禁止静默截断后继续宣称成功。

## 5. 目标架构

```text
转换后的源文档
  -> 提取检索查询特征
     - 文件名 / 标题 / Frontmatter
     - Markdown 标题
     - 高频中英文术语
     - 显式实体名和 [[Wikilinks]]
  -> WikiContextRetriever
     - 扫描允许参与问答的知识页面
     - 按 Markdown 标题分块
     - 计算确定性相关度
     - 做页面类型和来源多样性约束
  -> PromptBudget
     - 计算固定开销
     - 为输出和安全余量预留 token
     - 在 Wiki 上下文预算内选择 Top-K 章节
  -> IngestPromptContext
     - 选中页面路径
     - 选中标题 / 章节
     - 相关度和估算 token
     - 有界 Wiki 上下文文本
  -> render_prompt("ingest.md", ...)
  -> 最终 Prompt 硬校验
  -> call_ingest_llm / call_llm_main
```

## 6. 详细设计

### 6.1 最小配置项

在 `app/config.py` 和 `.env.example` 增加最小必要配置，不把每个内部权重都暴露成环境变量：

```env
# 当前 Ingest 模型实际使用的上下文窗口；必须与 DGX Ollama num_ctx 或云模型限制一致。
WIKI_BACKEND_INGEST_LLM_CONTEXT_WINDOW_TOKENS=32768

# 整个 Prompt 中允许 Wiki 相关上下文使用的最大 token 数。
WIKI_BACKEND_INGEST_WIKI_CONTEXT_MAX_TOKENS=6000

# 相关页面章节的最大候选数量，最终仍受 token 预算约束。
WIKI_BACKEND_INGEST_RETRIEVAL_TOP_K=12
```

现有 `WIKI_BACKEND_INGEST_LLM_MAX_TOKENS` 继续表示最大输出 token，不得与上下文窗口混用。

配置校验要求：

- 所有值必须大于 0。
- `INGEST_LLM_MAX_TOKENS + WIKI_CONTEXT_MAX_TOKENS` 必须小于上下文窗口。
- 必须为固定指令、当前源文档和安全余量保留空间。
- 配置不合法时应用启动失败，不能在首个 Ingest 任务中才暴露。

计划实施时应根据 DGX 上 `qwen3.6:35b` 的实际 `num_ctx` 调整示例值；上面的数值只是计划占位，不是已确认的生产参数。

### 6.2 Token 估算接口

新增一个小型、可注入测试替身的 token 估算接口，例如：

```python
class TokenEstimator(Protocol):
    def count(self, text: str) -> int:
        ...
```

实现原则：

1. 优先使用当前 LiteLLM/模型可验证的 tokenizer 能力。
2. 如果本地 Ollama 模型无法被 LiteLLM 正确识别，使用经过测试的保守估算，不得低估。
3. 估算策略和实际模型必须写入日志，但不记录 Prompt 正文。
4. 单元测试使用确定性 fake estimator，避免测试依赖外部模型或网络。

不得只使用字符数作为最终生产判断；字符数可作为 tokenizer 不可用时的保守 fallback，但必须留足安全余量并在 DGX 实测校准。

### 6.3 Prompt 预算计算

新增 `PromptBudget` 或等价值对象，至少记录：

- `context_window_tokens`
- `output_reserved_tokens`
- `safety_margin_tokens`
- `fixed_prompt_tokens`
- `source_tokens`
- `wiki_context_budget_tokens`
- `estimated_total_input_tokens`

预算顺序：

1. 先计算不含 Wiki 上下文的固定 Prompt 和源文档 token。
2. 从上下文窗口中扣除输出预留和安全余量。
3. 得到本次任务真实可用的 Wiki 上下文预算。
4. 真实预算不得超过 `WIKI_BACKEND_INGEST_WIKI_CONTEXT_MAX_TOKENS`。
5. 按相关度依次加入页面章节，直到下一个章节无法完整放入。
6. 渲染完整 Prompt 后再次计数，执行最终硬校验。

如果固定 Prompt加源文档已经超过输入预算：

- 不调用 LLM。
- 任务标记为 `failed`。
- 使用稳定错误分类，例如 `ingest_source_context_too_large`。
- 错误信息明确提示需要长文档分块，不把模型的 context overflow 原始异常直接暴露给用户。

### 6.4 Wiki 页面和章节切分

新增 `WikiContextRetriever`，不要继续在 `IngestService` 内堆积检索细节。

页面范围通过现有知识页面策略枚举，排除：

- `wiki/log.md`
- maintenance / health / lint 等运行报告
- graph 运行产物
- 其他现有策略明确不参与知识问答的文件

切分规则：

1. 读取 Frontmatter，提取 `title`、`type`、`tags`、`sources`。
2. 按 Markdown `##` / `###` 标题切成章节。
3. Frontmatter 和页面标题作为每个章节的检索元数据，不重复把完整 Frontmatter 塞入每个块。
4. 超长章节按段落边界继续切分。
5. 每个块保留：页面相对路径、页面标题、章节标题、正文、估算 token。
6. 不在句子、Markdown 表格行或代码围栏中间截断。

`overview.md` 按相同规则成为可检索章节，不再默认全文进入 Prompt。

`index.md` 用于提取页面目录、标题和简述；除非检索结果明确需要某个目录片段，否则不把其完整正文放入 Prompt。

### 6.5 首轮相关性算法

首轮采用完全本地、确定性的混合词法评分，不引入 embedding：

1. 标题、slug、Frontmatter tag 精确匹配：最高权重。
2. 新文档 Markdown 标题与候选标题重合：高权重。
3. 中英文关键词和字符 n-gram 重合：基础权重。
4. 新文档中的 `[[Wikilinks]]` 指向页面：高权重。
5. 已选页面的一跳 wikilink 邻居：小幅加权，用于补充已有关系。
6. 页面类型多样性：避免 Top-K 全被 Source 占满，至少允许相关 Entity/Concept 参与。
7. 修改时间只作为最终同分排序条件。
8. 路径作为最后稳定排序键，保证结果可复现。

检索查询特征必须从源文档确定性提取。首轮不额外调用 LLM 做查询改写，否则会增加成本、失败边界和模型耦合。

### 6.6 Top-K 与预算的关系

`TOP_K` 只是候选上限，不保证一定加入 K 个块。实际选择必须同时满足：

- 相关度高于最低内部阈值。
- 总 token 不超过本次 Wiki 上下文预算。
- 单个页面不能无上限占满预算。
- 同一页面优先保留最高分章节。
- 低相关候选不因“凑满 K 个”而进入 Prompt。

最终 Prompt 中每个上下文块使用统一边界，例如：

```text
## Relevant Wiki context

### wiki/entities/Example.md — Relationships
<selected section content>

---

### wiki/sources/older-but-relevant.md — Key Claims
<selected section content>
```

页面路径和章节标题必须保留，便于模型区分来源，也便于日志和测试验证选择结果。

### 6.7 空 Wiki 和低相关性处理

- Wiki 为空时继续使用当前首篇文档语义，不报错。
- 没有候选达到相关性要求时使用明确占位文本，不回退到“最近 5 篇”。
- `overview.md` 不存在时不创建伪造内容。
- 检索失败属于 Ingest 失败，不允许静默回退到无界全文上下文。

### 6.8 可观测性

每个 Ingest 任务至少记录以下结构化信息：

- provider 和 model。
- 上下文窗口、输出预留和安全余量。
- 固定 Prompt、源文档、Wiki 上下文、最终输入的估算 token。
- 候选块数量和最终选中块数量。
- 选中的页面相对路径、章节标题和相关度。
- 是否触发 tokenizer fallback。

禁止记录：

- Prompt 全文。
- 文档正文。
- API key。
- 不必要的敏感业务内容。

首轮不要求修改 MySQL schema；先使用结构化日志完成验证。若后续需要 UI 展示或长期审计，再单独评估是否持久化检索摘要。

## 7. 代码修改范围

预计修改：

- `app/config.py`
  - 新增 Ingest 上下文窗口、Wiki 上下文预算和 Top-K 配置。
  - 增加跨字段合法性校验。
- `.env.example`
  - 增加配置示例和 DeepSeek/Ollama 含义说明。
- `app/services/ingest_service.py`
  - 移除完整 `index.md`、完整 `overview.md` 和最近 5 篇 Source 的直接拼接。
  - 调用预算器和检索器。
  - 在 LLM 调用前执行最终 Prompt 硬校验。
- `app/services/ingest_prompt_budget.py`（建议新增）
  - token 估算协议和 Prompt 预算计算。
- `app/services/wiki_context_retriever.py`（建议新增）
  - 页面枚举、Markdown 分节、确定性相关性排序和预算内选择。
- `app/prompts/ingest.md`
  - 将 `wiki_context` 的含义明确为“预算内选中的相关 Wiki 章节”，不再暗示完整 Wiki 状态。
- `tests/test_ingest_service.py`
  - 更新 Prompt 构建相关测试。
- `tests/test_ingest_prompt_budget.py`（建议新增）
  - 预算边界和超限测试。
- `tests/test_wiki_context_retriever.py`（建议新增）
  - 相关性、切分、多样性和稳定排序测试。
- `tests/test_startup_dependencies.py`
  - 配置默认值与非法组合测试。
- `README.md`、`docs/ingest.md`、`docs/ingest-flow.md`
  - 同步新的 Prompt 上下文选择和超限错误语义。

不修改：

- `llm-wiki-agent` 源码。
- Quartz 代码。
- 生成的 `quartz/public/`。
- 当前 Ingest Wiki 写入和发布队列语义。

## 8. 实施阶段

### 阶段 0：用测试固定当前问题

先写失败测试，证明两个问题确实存在：

1. 构造超大 `index.md` 和 `overview.md`，证明旧 Prompt 随文件大小线性增长。
2. 构造 5 篇最近但无关的 Source，以及 1 篇较旧但标题、实体和关键词高度相关的 Source，证明旧实现选错上下文。
3. 构造相关 Entity/Concept，证明旧实现完全不会读取这些页面。

验证：测试在旧实现上失败，并且错误原因与目标问题一致。

### 阶段 1：实现 Prompt 预算

1. 增加配置和跨字段校验。
2. 实现 token estimator 接口和 fake。
3. 实现 `PromptBudget`。
4. 在最终 LLM 调用前做硬校验。
5. 增加源文档本身超限的稳定错误分类。

验证：无论 `index.md` / `overview.md` 增长多少，最终 Prompt 都不能超过配置上限；超限时零 LLM 调用。

### 阶段 2：实现相关页面检索

1. 实现知识页面枚举和 Markdown 章节切分。
2. 实现查询特征提取。
3. 实现确定性混合词法评分。
4. 实现类型多样性和稳定排序。
5. 在 token 预算内选择相关章节。
6. 删除“最近 5 篇”回退逻辑。

验证：旧但相关的页面能稳定排在最近但无关页面之前；Entity/Concept/Overview 相关章节可被选中。

### 阶段 3：接入 Ingest Prompt

1. 用结构化检索结果生成 `wiki_context`。
2. 渲染最终 Prompt 后再次计数。
3. 添加不含正文的结构化日志。
4. 保持现有 LLM JSON 解析和 Wiki 写入语义不变。

验证：现有正常 Ingest 测试继续通过，Prompt 断言改为检查“相关上下文”而不是“最近 Source 全文”。

### 阶段 4：DeepSeek 与 DGX Ollama 验证

1. Windows 单元测试使用 fake estimator 和 fake LLM，不调用真实模型。
2. DGX 确认 `qwen3.6:35b` 实际 `num_ctx` 和运行参数。
3. 使用相同文档分别构造 DeepSeek 和 Ollama Prompt，确认都在预算内。
4. 先运行不写正式 Wiki 的测试副本或临时知识库。
5. 对比旧、新上下文选择结果和最终 Source 页面准确性。

验证：本地模型不发生 context overflow；旧的相关页面能够被召回；新增无关页面不会显著改变选中上下文。

### 阶段 5：文档同步

更新 README 和 Ingest 文档，明确：

- `index.md` / `overview.md` 不再默认全文进入 Prompt。
- Wiki 上下文来自预算内相关章节。
- `INGEST_LLM_MAX_TOKENS` 是输出预算，不是上下文窗口。
- 源文档超限会明确失败，首轮不会静默截断。
- DeepSeek 和 Ollama 必须分别配置并验证实际上下文窗口。

## 9. 测试矩阵

### 9.1 Prompt 预算

- 空 Wiki。
- 小型 Wiki。
- 10,000 条 `index.md` 条目。
- 超长 `overview.md`。
- 固定 Prompt 接近预算边界。
- 源文档恰好可容纳。
- 源文档超过预算 1 token。
- Wiki 上下文预算为最小合法值。
- 非法配置组合导致启动失败。
- tokenizer 不可用时使用保守 fallback。

### 9.2 检索相关性

- 较旧但标题精确匹配的 Source 胜过最近无关 Source。
- Entity 精确匹配。
- Concept 精确匹配。
- `overview.md` 仅选中相关章节。
- 中英文混合标题和正文。
- 中文字符 n-gram 检索。
- 相同分数时结果顺序稳定。
- 单一大页面不能占满全部预算。
- 低相关候选不为凑满 Top-K 而加入。
- runtime report 和 log 不参与检索。

### 9.3 Ingest 集成

- 最终 Prompt 不含完整大 `index.md`。
- 最终 Prompt 不含完整大 `overview.md`。
- 最终 Prompt 包含选中页面路径和章节标题。
- 超限时 LLM caller 调用次数为 0。
- 正常 JSON 解析和 Wiki 写入行为不变。
- LLM 截断、JSON 修复和 schema 错误测试继续通过。
- manual / scheduled 来源字段行为不变。

## 10. 验收标准

完成本计划必须同时满足：

1. 增加任意数量的无关 `index.md` 条目，最终 Prompt 仍不超过配置的输入预算。
2. `overview.md` 再长也不会默认全文进入 Prompt。
3. 不再存在硬编码“最近 5 篇 Source”作为 Wiki 上下文的逻辑。
4. 较旧但相关的 Source/Entity/Concept 能稳定胜过最近但无关的 Source。
5. 最终选中上下文同时受相关度、Top-K 和 token 预算约束。
6. 源文档本身超限时，在 LLM 调用前以稳定错误失败，不静默截断。
7. 日志能够说明预算使用量和选中了哪些路径/章节，但不记录正文。
8. Windows 对应单元测试和完整测试集通过。
9. DGX 上读取并确认 `qwen3.6:35b` 实际上下文配置，完成至少一组代表性文档验证。
10. DeepSeek 当前 Ingest 行为无回归，Quartz 发布边界不变。

## 11. 建议验证命令

Windows：

```powershell
.venv\Scripts\python.exe -m unittest tests.test_ingest_prompt_budget -v
.venv\Scripts\python.exe -m unittest tests.test_wiki_context_retriever -v
.venv\Scripts\python.exe -m unittest tests.test_ingest_service -v
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

真实 Ingest 验证必须使用临时或明确可回滚的 Wiki 副本，先确认 LLM、MySQL、Wiki 写入和 Quartz 发布副作用范围。

## 12. 风险与回滚

### 风险

- 词法检索对同义词和隐含语义的召回不如 embedding。
- token 估算与 Ollama 实际 tokenizer 可能有偏差。
- 预算过小可能遗漏必要历史信息。
- 预算过大可能在本地模型上增加延迟和内存压力。
- Markdown 章节切分若处理表格、代码围栏不当，可能破坏上下文完整性。

### 缓解

- 使用保守估算和安全余量。
- 保留选中路径、章节和分数日志，便于调参。
- 用中英文、长文档、历史相关页面建立固定回归样本。
- 首轮以确定性词法检索为基线，只有基线指标不足时再单独设计 embedding 阶段。

### 回滚

- 代码变更应拆成“预算器”“检索器”“Ingest 接入”三个可独立审查的提交。
- 回滚时可以恢复旧 Prompt 构建代码，但不得以恢复无界上下文作为长期方案。
- 配置和文档必须与实际启用的实现保持一致。

## 13. 后续独立计划

本计划完成后，再根据 DGX 验证结果决定是否建立以下独立计划：

1. 长文档按章节/页分块提取与聚合。
2. 本地 embedding 或混合检索。
3. Ingest 专用 Ollama 模型配置和 DeepSeek fallback。
4. Entity/Concept/Overview 的 patch 合并、staging 和回滚。
5. Ingest 准确率评测集与 `needs_review` 质量门。
