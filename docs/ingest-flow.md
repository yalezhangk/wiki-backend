# 当前 Ingest 工作流

本文只描述当前 `wiki-backend` 代码已经实现的 Ingest 行为。它不是目标架构、
也不是后续可能增加的长文分块、相关 Wiki 检索或多阶段 Agent 方案。

## 总览

当前 Ingest 是单进程内存队列中的单 worker 工作流：对一份文档构造一次完整 Prompt，
调用一次 Ingest 模型以取得完整 JSON；后端校验并写入 Wiki，最后加入 Quartz 发布队列。

```text
POST /api/ingest/jobs
  -> 校验并保存上传文件
  -> MySQL 创建 queued job（保存 ingest_model）
  -> 当前进程内 Queue
  -> daemon worker
       -> 转 Markdown / PDF OCR（需要时）
       -> 文本质量预检
       -> 拼完整 Prompt
       -> 一次 LLM 完整 JSON 生成
       -> JSON 解析；非截断错误时可修复一次
       -> Pydantic 结果校验
       -> 写 Source / Entity / Concept / index / log
       -> 断链与索引校验
  -> MySQL 标记 succeeded 或 failed
  -> succeeded 时加入 Quartz 发布队列
```

`202 Accepted` 只表示文件已保存且任务已进入内存队列，不表示 Wiki 已写入；
`succeeded` 只表示 Wiki 写入已完成，只有 `publication.status=published` 才表示
Quartz 静态站点已经更新。

## 1. 接口、任务与队列

入口为 `POST /api/ingest/jobs`，使用 `multipart/form-data`：

- `file` 必填；
- `auto_convert` 默认 `true`；非 Markdown 在设为 `false` 时被拒绝；
- `trigger` 默认 `manual`；`scheduled` 必须附带 HTTP/HTTPS `source_url`；
- 路由调用 `IngestService.create_job()`，成功后返回任务对象。

创建任务时会：

1. 去掉浏览器传来的客户端目录，检查文件名、扩展名和触发方式。
2. 使用 `normalize_document_name()` 建立全局文档主名唯一键，检查 manual/scheduled 冲突。
3. 使用 `_safe_filename()` 生成落盘文件名。
4. 将 manual 文件写入 `raw/uploads/manual/`，scheduled 文件写入 `raw/uploads/scheduled/`。
5. 以 64 KiB 分块独占新建文件，校验文件大小、声明 MIME 类型及支持格式的签名/容器结构。
6. 创建 MySQL `ingest_jobs` 记录，初始为 `queued/uploaded/0%`。
7. 保存此时解析出的 `ingest_model`，并将 job ID 放入服务实例内的 `Queue[int]`。

worker 是 daemon 线程，不是 MySQL 持久化队列。服务重启时未处理的内存 Queue 项不会由
该队列本身恢复。任务只保存模型标识，不保存完整的模型配置、输出预算、Prompt 版本或 API 地址；
worker 会按照该模型标识和运行时配置重新构造 profile。

## 2. 文档转换与内容预检

worker 取出 job 后先标记 `running`。Markdown 直接读取；其他格式通过 MarkItDown 转成
Markdown。PDF 先检查文本层：有文本层时优先 MarkItDown，失败时回退 PyMuPDF；无文本层或
原生转换质量不足时使用 OCR（Marker 可用且已启用时优先，否则使用 RapidOCR）。

转换/读取后，后端会拒绝：

- 空文本；
- 控制字符或替换字符比例达到 1% 的文本；
- 非 Markdown 转换后可读字母数字字符过少的文本；
- 加密、损坏或 OCR 无法获得足够可读内容的 PDF。

非 Markdown 任务在转换时进度为 `converting/10%`；可用 Markdown 准备完成后进入
`extracting/35%`。

## 3. Prompt 的实际组成

当前只有一个主 Prompt，模板为 `app/prompts/ingest.md`，其中嵌入
`app/prompts/ingest_instructions.md`。它以单条 `user` message 发送给模型，组成顺序为：

1. 固定 Ingest 角色和 JSON 输出合约。
2. 完整 `wiki/index.md`。
3. 完整 `wiki/overview.md`。
4. 按修改时间倒序的最近五篇 `wiki/sources/*.md` 全文。
5. 本次上传文档的完整 Markdown。
6. 当天日期。

因此当前实现不是按相关性检索 Wiki，也不做来源文档的 chunk/reduce；`index.md`、
`overview.md` 和最近五个 Source 越大，Prompt 就越大。

Prompt 要求模型返回一个 JSON 对象：成功结果包含 `title`、`slug`、完整
`source_page`、`index_entry`、可选 `entity_pages`/`concept_pages`、与新页一一对应的
`entity_index_entries`/`concept_index_entries`、`contradictions` 和
`log_entry`；无法可靠处理时返回 `ingest_status="failed"` 与 `ingest_error`。

说明文件中曾描述“高层综合有实质变化时可返回 `overview_update`”，但外层 JSON 合约明确要求
始终为 `null`。后端仍会接受非空 `overview_update`，并直接覆盖 `wiki/overview.md`。

## 4. 模型档案与思考模式

Ingest 只接受服务端白名单模型：

| 模型标识 | 思考模式 | 本地上下文预检 |
| --- | --- | --- |
| `deepseek/deepseek-v4-flash` | 强制关闭 | 不做本地硬限制 |
| `deepseek/deepseek-v4-pro` | 强制关闭 | 不做本地硬限制 |
| `ollama_chat/qwen3.6:35b` | 强制关闭 | 做 65,536 token 预算检查 |

DeepSeek V4 的 Ingest profile 固定为 `reasoning_effort="none"`，并通过
`extra_body={"thinking": {"type": "disabled"}}` 显式关闭供应商默认思考。原因是
Ingest 的成功条件是得到一个完整、可机器解析的 JSON；思维链若与最终内容共用 completion
预算，可能在 JSON 输出前耗尽预算。

Qwen Ingest 同样固定为 `reasoning_effort="none"`。这仅限当前结构化写入调用；当前代码
没有另行实现“可思考的分析阶段 + 直答写入阶段”。

调用统一经过 LiteLLM，关键参数为：

```python
completion(
    model="<provider>/<model>",
    messages=[{"role": "user", "content": prompt}],
    max_tokens=WIKI_BACKEND_INGEST_LLM_MAX_TOKENS,
    temperature=WIKI_BACKEND_LLM_MAIN_TEMPERATURE,
)
```

当前默认输出预算为 `WIKI_BACKEND_INGEST_LLM_MAX_TOKENS=8192`。这是一整次
completion 的输出上限，必须容纳 Source、Entity、Concept、索引项和日志项的全部 JSON。

## 5. Token 预算和上下文检查

模型调用前会在最终 `render_prompt()` 后执行 `_assert_prompt_fits_model_context()`。没有可用的
厂商 tokenizer 时，它使用保守估算：中日韩等非 ASCII 字符约按 1 字符/1 token，ASCII
字母数字约按 4 字符/1 token；该值不是精确 tokenizer 结果。

- `deepseek/deepseek-v4-pro`：本地输入上限 131,072、输出上限 16,384、安全余量 16,384。
- `deepseek/deepseek-v4-flash`：本地输入上限 98,304、输出上限 8,192、安全余量 8,192。
- `ollama_chat/qwen3.6:35b`：本地输入上限 49,152、输出上限 8,192、安全余量 8,192。

所有档案都要求：

  ```text
  估算输入 <= 档案 max_input_tokens
  估算输入 + Ingest 输出预算 + 档案 safety_margin_tokens <= context_window_tokens
  ```

  不满足时，在调用模型前以 `ingest_source_context_too_large` 失败。

转换后 Markdown 估算不超过 24,576 tokens 才会进入单次完整来源路径；正文绝不为 Wiki
上下文而截断。超过该值会在调用 LLM 前以 `ingest_source_context_too_large` 失败。长文
chunk/reduce 尚未实施。

Wiki 上下文在 `wiki_lock` 内拍摄只读快照，仅检索 `overview.md`、`sources/`、`entities/`
和 `concepts/` 的命中小节；`index.md` 只保留目录角色，绝不作为完整上下文。检索按来源
标题、词语和 WikiLink 进行稳定词法排序，最多使用 16,384 tokens，零命中或预算为零会渲染
显式空证据占位，不会回退到最近页面。每个片段带路径、标题和快照 hash 前缀；日志只记录
预算与候选元数据，不记录正文。

## 6. JSON 解析、截断和重试

收到模型响应后，后端会：

1. 记录 `finish_reason`、最终内容长度及是否存在推理字段，但不记录推理正文。
2. `finish_reason="length"` 时立即标记为输出截断。
3. 去掉最外层可选 JSON Markdown fence，从第一个 `{` 用 `raw_decode()` 解析对象。
4. 使用 Pydantic `IngestLLMResult` 或 `IngestLLMFailure` 校验业务结构。

异常处理如下：

| 情况 | 行为 |
| --- | --- |
| `finish_reason=length` 或明显未闭合 JSON | 不修复，任务失败；若有部分内容，保存 `*.truncated.llm-response.txt` |
| 网络超时、连接错误、429、5xx、空最终内容 | 等待 0.25 秒后重试一次 |
| 非截断的无效 JSON | 保存初始响应，构造 JSON repair Prompt，再调用模型一次 |
| repair 后仍无效 | 任务失败，保存 retry 响应 |
| JSON 结构不符合 Pydantic | 任务失败，保存 `*.schema.llm-response.txt` |

JSON repair 只携带紧凑契约和无效响应，要求“只修复 JSON 结构、不增加事实”；它不会重传
来源正文、Wiki 片段或原主 Prompt。

## 6.1 既有知识页的受控更新

新建 Entity/Concept 仍可由模型提交完整 Markdown，但相同路径已存在时后端拒绝覆盖。既有页只能
对本次检索快照中出现的路径提交 `entity_patches` 或 `concept_patches`：每个 patch 必须带匹配的
SHA-256 `base_hash`，并且只能追加一个新的二级小节，或替换一个已存在二级小节的正文。后端拒绝
过期 hash、未检索路径、重复目标小节和 `Sources`/`Related` 的替换；因此 Frontmatter、既有来源
清单和关联不会被一次普通 Ingest 静默重写。

当前没有使用供应商 `response_format`、JSON Schema 或 function calling 来约束模型输出；
约束来自 Prompt、JSON 解析和 Pydantic 后验校验。

## 7. Wiki 写入和后处理校验

Pydantic 校验成功后，在 `wiki_lock` 内执行写入：

1. `slug` 必须匹配受限字符规则，新的 Source 页面不得覆盖已有 `wiki/sources/<slug>.md`。
2. Entity/Concept 页面必须是对应目录下的 `.md`，拒绝绝对路径、`..` 等越界路径。
3. 通过临时文件替换进行原子写入。
4. Source Frontmatter 由后端纠正：强制 `title/type/tags/date`，manual 只保留 `source_file`，
   scheduled 只保留 `source_url`。
5. `index_entry` 插入 `wiki/index.md` 的 `## Sources`；每个新建 Entity/Concept 都必须分别提供
   对应分区的索引条目，后端将其写入 `## Entities` 或 `## Concepts`。
6. `log_entry` 写入 `wiki/log.md`；若 `overview_update` 非空则覆盖 `wiki/overview.md`。

写完后，`_validate_ingest()` 仅检查本次知识页：

- 页面中的 `[[Wikilink]]` 目标是否存在；
- 页面文件名是否出现在 `index.md` 的全文。

校验结果保存到任务的 `validation.broken_links` 和 `validation.unindexed`，但当前不会因为
断链而回滚或标记任务失败。新建 Entity/Concept 在写入前缺少对应索引条目会直接失败；
`unindexed` 仍作为历史数据和异常写入的兜底信号。

## 8. 成功、失败和发布

成功时，后端写入创建/更新页面、矛盾列表和校验结果，将任务标记为：

```text
status=succeeded
stage=completed
progress_percent=100
```

若 `PublishService` 可用，随后将该 job 加入 Quartz 的合并发布队列。发布异步进行，不能把
Ingest 的 `succeeded` 解释为站点已更新。

任意未处理异常都会标记 job 为 `failed`，保留失败前的阶段和进度，并删除该任务 `source_path`
指向的上传文件。转换出的 Markdown 与模型诊断文件不等于 `source_path`，不会由这一清理逻辑
统一删除。

## 当前实现的边界

- 单次完整 Prompt、单次主生成；没有长文分块、分阶段抽取、reduce 或事实草稿。
- Wiki 上下文采用固定“完整 index + 完整 overview + 最近五 Source”，不是相关性检索。
- DeepSeek 没有本地输入 token 硬上限；输出只有单次 completion 上限。
- JSON 依靠 Prompt 和后验解析/Pydantic；没有供应商级 JSON Schema 约束。
- 链接和索引校验是报告型，不是阻断型质量门。
- 队列为当前进程内存队列，不能替代持久化、可恢复的任务调度。
