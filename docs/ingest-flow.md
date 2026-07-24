# Ingest 文档上传与入库流程

本文依据当前 `wiki-backend` 实现梳理 Ingest 的运行流程、任务状态和同名文件处理方式。

## 范围与边界

Ingest 由 `wiki-backend` 提供 HTTP 接口，使用 MySQL 持久化任务元数据，并在
`WIKI_AGENT_REPO_PATH` 指向的 agent 仓库内读写 `raw/uploads/` 与 `wiki/`。

Ingest 成功只表示 Wiki 文件已写入和校验完成；它不会自动重建或发布 Quartz
静态站点。

## 调用入口与异步模型

客户端通过 `POST /api/ingest/jobs` 以 `multipart/form-data` 上传 `file`，可选参数
`auto_convert` 默认为 `true`。

路由将工作交给 `IngestService.create_job()`：文件成功保存、MySQL 任务创建且任务放入
内存队列后，即返回 `202 Accepted`。这表示任务已入队，不表示资料已进入 Wiki。

应用启动时会创建一个 `IngestService`；服务内部使用一个 daemon worker 线程消费
`Queue[str]` 中的 job ID。因此当前队列是进程内队列，而不是由 MySQL 驱动的可恢复任务队列。

```text
POST /api/ingest/jobs
  -> 校验并保存上传源文件
  -> MySQL 创建 queued 任务
  -> 内存 Queue
  -> worker 转换、LLM 提取、写 Wiki、校验
  -> MySQL 更新 succeeded 或 failed
```

## 上传接收与源文件落盘

`create_job()` 依次执行以下步骤：

1. 取 `Path(file.filename).name`，丢弃浏览器携带的客户端目录。
2. 检查文件名不为空、扩展名受支持；非 `.md` 文件在 `auto_convert=false` 时被拒绝。
3. 使用 `_safe_filename()` 生成保存名：替换 Windows 非法字符、将连续空白替换为 `-`、去除首尾 `.`/`-`，最多保留 180 个字符。因此“原文件名”指安全处理后的文件名，而不是不经处理的客户端字节串。
4. 目标路径为 `raw/uploads/<保存名>`。例如上传 `报告.pdf`，源文件保存为 `raw/uploads/报告.pdf`；不再添加日期、随机数等前缀。
5. 使用 64 KiB 分块以独占新建模式写文件；校验总大小、声明的 MIME 类型，以及 PDF、Office、EPUB、XLS、RTF、WAV、MP3 等格式的文件签名或容器结构。
6. 若大小、类型或签名校验失败，删除本次部分落盘文件，不创建任务。

上传成功后创建 `ingest_jobs` 记录，初始值为：

- `status=queued`
- `stage=uploaded`
- `progress_percent=0`
- `source_path=raw/uploads/<保存名>`

## 后台处理与 Wiki 写入

worker 取到 job ID 后，将任务标为 `running`，再按以下顺序执行：

1. **转换**：Markdown 直接处理；其他受支持格式通过 `MarkItDown` 转换，并写入源文件同目录、同基名的 `.md`。例如 `报告.pdf` 转换为 `报告.md`。
2. **提取**：读取 Markdown 内容，并附带 `wiki/index.md`、`wiki/overview.md` 与最近五篇 source 页面组成 Prompt，调用主 LLM。
3. **结果校验**：LLM 必须返回符合 `IngestLLMResult` 的 JSON。首次 JSON 无法解析时会请求模型修复一次；明确截断的响应直接失败。失败响应会在 `uploads/` 旁写入带 job ID 的诊断文本。
4. **写 Wiki**：校验 `slug` 和 Entity/Concept 输出路径后，原子写入 `wiki/sources/<slug>.md`、`wiki/entities/*.md`、`wiki/concepts/*.md`；必要时覆盖 `wiki/overview.md`，并更新 `wiki/index.md` 与 `wiki/log.md`。
5. **后处理校验**：检查本次改动页面中的断裂 `[[wikilinks]]`，并检查新页面是否出现在索引中。
6. **完成或失败**：成功时写入创建/更新页面、矛盾列表和校验结果，并标为 `succeeded`；任何处理异常都会记录错误并标为 `failed`。

## 阶段与进度

| 阶段 | 进度 | 含义 |
| --- | ---: | --- |
| `uploaded` | 0 | 源文件已保存，任务已入队 |
| `converting` | 10 | 非 Markdown 文件正在转换 |
| `extracting` | 35 | 正在读取 Markdown 并调用 LLM 提取知识 |
| `writing_wiki` | 65 | 正在写入 Wiki 页面、索引和日志 |
| `validating` | 85 | 正在检查断链和未索引页面 |
| `completed` | 100 | Wiki 写入及校验完成 |

失败任务保留失败前最后一个 `stage` 和 `progress_percent`，便于定位失败边界。

## 同名文件判断

上传源文件的同名判断由文件系统完成，不查询 MySQL：

1. 保存前先检查 `raw/uploads/<保存名>` 是否存在。存在即抛出 `IngestConflictError`。
2. 真正落盘时使用 `open("xb")` 独占创建。即使两个请求同时通过前置检查，后到请求也会因 `FileExistsError` 转为同一个冲突错误。
3. API 将该错误映射为 HTTP `409 Conflict`，响应体中的 `detail` 为“上传文件已存在，请修改文件名后重试: ...”。

因此，以下情况会冲突：

- 已有 `raw/uploads/报告.pdf`，再次上传 `报告.pdf`。
- 两个原始文件名在 `_safe_filename()` 后变为相同保存名，例如 `a b.pdf` 与 `a-b.pdf`。

扩展名、`auto_convert` 等基础校验发生在同名检查之前；不受支持的文件会先返回 `422`，而不是 `409`。

### 转换 Markdown 的独立边界

当前同名冲突检查仅覆盖上传的**源文件**。非 Markdown 的转换结果由
`source.with_suffix(".md")` 得出，并通过原子替换写入；该转换目标目前不会先进行
同名冲突检查。

例如：`raw/uploads/报告.pdf` 不存在但 `raw/uploads/报告.md` 已存在时，上传
`报告.pdf` 可以通过源文件冲突检查，转换阶段会覆盖现有的 `报告.md`。这与“源文件
同名上传返回 409”是两个独立的行为边界。

## 主要实现位置

- `app/main.py`：服务生命周期中创建 `IngestService`。
- `app/api/ingest.py`：`POST /api/ingest/jobs`、任务查询路由及 HTTP 错误映射。
- `app/services/ingest_service.py`：上传校验、队列 worker、转换、LLM 提取、Wiki 写入和同名处理。
- `app/storage/mysql.py`：`ingest_jobs` 创建、进度更新、成功和失败状态持久化。
- `app/schemas/ingest.py`：任务状态、阶段、LLM 返回结构和 API 响应模型。
- `app/prompts/ingest.md`：入库使用的 LLM 输出契约。
