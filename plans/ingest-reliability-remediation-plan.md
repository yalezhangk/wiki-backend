# Ingest 可靠性修复计划

## 背景与本次故障证据

UI 通过 `POST /api/ingest/jobs` 上传 PDF 后，任务在 `extracting` 阶段失败。排查保存到 `raw/uploads/` 的两份 LLM 原始响应后确认：两份内容都以 JSON 对象开头，但均在 JSON 字符串内部中止，缺少闭合的引号和 `}`。

- 初始响应中止于 `concept_pages[1].content`。
- JSON repair 的重试响应中止于 `overview_update`。
- 中间还发生过一次 `LLM returned an empty response`；这是独立的瞬时空响应。

因此，`GET /api/ingest/jobs/{job_id}` 返回 HTTP 200 仅说明任务状态可查询，不表示导入成功。此次根因不是 PDF 签名校验、MarkItDown 转换、轮询接口或 Wiki 写入；Wiki 写入发生在 LLM 响应通过 JSON 与 Pydantic 校验之后，本次未执行。

## 当前流程

```text
POST /api/ingest/jobs
  -> 校验扩展名、MIME、签名与文件大小
  -> 保存到 raw/uploads/
  -> MySQL 创建 queued ingest_jobs 记录
  -> 进程内 Queue 入队
  -> daemon worker 取任务
  -> 非 Markdown 文件由 MarkItDown 转为 Markdown
  -> 读取完整 Markdown，拼接 Wiki 上下文和 ingest Prompt
  -> 调用主 LLM
  -> 解析 JSON；失败时再生成一次 JSON repair
  -> Pydantic 校验 IngestLLMResult
  -> 逐文件写入 source/entity/concept/overview/index/log
  -> 写入任务成功或失败状态
```

当前单轮 LLM 输出需要同时包含完整 `source_page`、多个 entity/concept 页面、可能很长的完整 `overview_update`、索引条目和日志条目。对于大型 PDF，这个输出形态超过单轮预算的概率很高。

## 已确认的问题

### P0：单轮 LLM 输出没有预算，且固定为 8192 tokens

`IngestService._call_llm_with_retry()` 固定传入 `max_tokens=8192`，绕过了现有的 `WIKI_BACKEND_LLM_MAIN_MAX_TOKENS`。同时 `IngestLLMResult` 对页面数量和各字段内容长度没有约束，Prompt 又允许生成完整 overview。这导致输出被截断后必然无法形成合法 JSON。

这也是本次失败的直接原因。

### P1：重试策略把不同类型的失败混为一谈

当前 `_call_llm_with_retry()` 对所有 `Exception` 重试一次；`_parse_llm_result_with_repair()` 在 JSON 无效时再调用一次带修复提示的 LLM。因此最坏会发起四次模型调用：初始调用两次，加 JSON repair 两次。

短暂的网络异常、连接重置、临时限流或空响应可以重试；以下情况不应按同一策略重试：

- `finish_reason=length` 或其他明确的输出截断；
- API key、模型名、参数配置错误；
- 模型不支持的能力；
- 已得到完整 JSON、但不符合 Pydantic schema 的结果。

当前 JSON repair 只是在完整原始 Prompt 后追加一句“仅返回 JSON”，没有把上次生成结果交给模型修补；它不能解决“输出内容过长”。

### P1：任务队列不具备重启恢复能力

worker 使用进程内 `Queue[str]` 和 daemon thread。MySQL 只保存任务状态，不保存或恢复队列消费位置。进程重启时，已经写入 MySQL 的 `queued` / `running` 任务不会自动重新入队，会永久停留在非终态。

当前实现也不适合多个 Uvicorn worker 或多个后端副本：每个进程各自拥有内存队列，且没有跨进程 Wiki 写入锁。

### P1：多文件 Wiki 写入没有事务或回滚

`_write_ingest_result()` 依次写 source、entity、concept、overview、index、log。单个文件使用原子替换，但整个任务没有事务边界。中间任一写入失败会保留已成功写入的部分文件，而任务最终标为 failed。

### P2：LLM 输入和输出均缺少大小控制

上传大小上限为 10 MiB，但 PDF 转 Markdown 后的字符数没有限制；Prompt 还可能包含 index、overview 和最近五份 source。超长输入可能超模型上下文、显著增加延迟和费用。

输出端也没有限制 entity/concept 页面数量、每页篇幅、source page 篇幅或 overview 篇幅。

### P2：LLM 结果校验与诊断边界不完整

JSON 解析成功后，`IngestLLMResult.model_validate()` 若失败，不会保存原始响应，也不会尝试结构化 repair。

`_parse_json_from_response()` 通过正则查找首尾 `{...}`：对截断 JSON 会给出“未包含 JSON 对象”，不能准确表达“JSON 在字符串中被截断”；对夹杂其它大括号的模型文本也不够稳健。

`app/llm_config.py` 只读取 `message.content`，没有检查或记录模型返回的 `finish_reason`，丢失了识别截断的关键证据。

### P2：上传与任务创建之间可能留下孤儿文件

上传文件先成功落盘，随后才创建 MySQL 任务。如果数据库写入失败，文件不会删除。文件名只使用“秒级时间戳 + 原始文件名”，相同文件在同一秒上传会得到 409 冲突。

### P2：失败调试响应的保留策略缺失

失败时完整 LLM 输出写在 `raw/uploads/*.llm-response.txt`，其中可能包含原始文档摘要和业务信息。当前没有保留期限、访问边界或清理机制；失败详情还会把调试文件相对路径保存到任务错误信息。

### P3：Markdown 编码与压缩容器资源限制不足

原始 `.md` 文件在后续阶段直接按 UTF-8 读取，上传时没有验证 UTF-8 编码。DOCX/PPTX/XLSX/EPUB 仅检查压缩包中的关键目录或 mimetype，未限制解压后的总大小；公开写接口场景还需要将其纳入上传资源防护。

## 修复顺序

### 阶段 1：解决本次截断并改善可观测性

目标：大型文档失败时有准确原因，且可在不改变 Wiki 写入语义的前提下调节输出预算。

1. 新增 `WIKI_BACKEND_INGEST_LLM_MAX_TOKENS`。
   - 在 `app/config.py` 提供 `ingest_llm_max_tokens`。
   - 默认值先保持 `8192`，确保升级不改变现有部署行为。
   - 同步 `.env.example`、README 与配置测试。
   - 仅在确认当前模型支持后，将部署值调高到 `12288` 或 `16384`；不得盲目提高。
2. 在 `app/llm_config.py` 提取并检查 `finish_reason`。
   - `length` 必须转换为专用“响应截断”异常，日志记录模型、请求 token 上限和 finish reason，但不记录 Prompt 或密钥。
   - 对 provider 未返回 finish reason 的情况，保留现有内容校验，并在 JSON 解码时识别不完整 JSON。
3. 将 LLM 重试限定为瞬时故障。
   - 对超时、连接错误、临时限流和空响应最多重试一次，使用短退避。
   - 对截断、认证/配置错误、schema 错误不进行相同 Prompt 的盲重试。
4. 完善失败诊断。
   - JSON 解析与 Pydantic 校验失败都保存受控调试信息。
   - 在 job error 中保存稳定的错误类别和简短面向 UI 的说明；调试文件路径只写服务日志，或改为受控诊断 ID。

验证：

- 单元测试覆盖 `finish_reason=length`、空响应重试一次、认证错误不重试、非法 JSON、截断 JSON、schema 不匹配 JSON。
- 使用 fake LLM 验证 token 配置确实传入 ingest 调用。
- 运行 `.venv\Scripts\python.exe -m unittest discover -s tests`。

### 阶段 2：为单轮 ingest 加输入与输出预算

目标：避免把大型 PDF 与整个知识库更新压进一个不可预测的 JSON 响应。

1. 为 Markdown 源文档设置字符或 token 预算；超过预算时按标题或页段切块，而不是静默截断。
2. 限制 Wiki 上下文的总预算，而非分别裁剪多个文件后再无上限拼接。
3. 收紧 Prompt 输出契约。
   - 限制 source page、entity/concept 页面数量及每页长度。
   - 初始入库不应要求重写一整份 `overview.md`；可以令 `overview_update` 默认 `null`，或只输出明确的短增量候选。
4. 对超过单轮预算的资料采用分阶段生成。
   - 第一阶段：写入来源页、索引条目、日志与结构化候选。
   - 后续阶段：按候选逐页生成 entity/concept，必要时单独更新 overview。
   - 每个阶段都必须有独立任务状态、失败原因与可恢复边界。

验证：

- 构造长 Markdown fixture，验证不会把超过预算的输入直接发送到模型。
- 构造大量 entity/concept 的 fake 输出，验证会被边界校验拒绝或正确分阶段。
- 对现有小 Markdown 导入保持成功和返回字段兼容。

### 阶段 3：保证任务与文件写入的一致性

目标：重启、写入异常和部署扩容不会遗留卡死任务或半完成 Wiki。

1. 在应用启动时处理历史非终态任务。
   - 明确策略：安全地重入队 queued 任务；running 任务标记为 interrupted 后允许显式重试，或在具备幂等设计后恢复。
   - 不得简单地把所有 running 任务当作成功。
2. 引入任务幂等与并发保护。
   - 记录 attempt 次数、最后错误类别与 worker 领取时间。
   - 部署维持单 worker 前提，或增加 MySQL 领取锁/分布式锁后再支持多进程。
3. 为 Wiki 写入增加任务级事务语义。
   - 先在同文件系统的 staging 目录准备所有文件。
   - 替换前记录原文件备份或 manifest；任一步失败时恢复原状态。
   - 任务成功后再提交最终状态；对进程崩溃保留可清理的 staging 信息。

验证：

- 测试启动恢复 queued/running 任务的策略。
- 模拟第 N 个文件写入失败，验证 Wiki 完全回滚。
- 在单 worker 与预期的重启场景中验证不会重复或丢失任务。

### 阶段 4：上传、隐私与运维收尾

目标：收紧资源与敏感数据边界。

1. MySQL 创建任务失败时删除刚写入的上传文件。
2. 用 UUID 或安全的冲突消解策略生成 stored filename，避免同秒同名冲突。
3. 上传 `.md` 时验证 UTF-8；压缩格式增加解压总量和条目数限制。
4. 明确 `.llm-response.txt` 的权限与保留时间；增加清理任务或在成功/过期后安全删除。
5. 审查面向 UI 的错误信息，避免暴露内部文件布局、原始文件名以外的服务端细节。

验证：

- 数据库失败不遗留上传文件。
- 并发同名上传均获得独立任务或返回明确、可预期的结果。
- 非 UTF-8 Markdown、超大压缩包和异常容器均被安全拒绝。

## 建议的实施边界

阶段 1 可以独立提交，直接改善本次问题的可诊断性和配置正确性。阶段 2 是解决大文档稳定性的必要工作；仅调大 token 不应视为完成。阶段 3 涉及状态机和文件一致性，应单独设计、单独测试，不与 Prompt 调整混在同一次变更中。阶段 4 可在前述稳定性完成后收尾。

每个阶段完成后都应运行：

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests
```

涉及真实 PDF、LLM、MySQL 或 Wiki 写入的验证，必须先使用隔离目录、fake storage / fake LLM；只有在明确批准后才对真实知识库执行端到端导入。
