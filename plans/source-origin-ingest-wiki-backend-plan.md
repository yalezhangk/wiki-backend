# `wiki-backend`：Ingest 原始来源与全局重名控制执行计划

## 线程目标

在 `wiki-backend` 中实现以下能力：

- manual/scheduled 分目录保存；
- 全局文档主名唯一；
- scheduled `Source URL` 提取和持久化；
- manual `source_file`、scheduled `source_url` 的确定性 Source frontmatter；
- 只迁移历史 manual 数据；
- 为 Quartz 原文件 emitter 提供构建期知识根路径。

本线程不得修改 `quartz` 代码，不得修改 `llm-wiki-agent/tools/*`。

## 主要改动文件

```text
app/schemas/ingest.py
app/api/ingest.py
app/services/ingest_service.py
app/services/scheduled_ingest_service.py
app/services/publish_service.py
app/storage/mysql.py
app/prompts/agent_instructions.md
tests/test_ingest_api.py
tests/test_ingest_service.py
tests/test_scheduled_ingest_service.py
tests/test_publish_service.py
tests/test_mysql_integration.py
README.md
AGENTS.md
```

迁移工具使用项目内 `.venv`，代码放在仓库既有脚本/工具约定位置；不要使用系统 Python。

## 1. Schema 与字段

为 `ingest_jobs` 增加：

```text
document_name_key VARCHAR(255) NULL
source_url VARCHAR(2048) NULL
```

`document_name_key`：后端从 `original_filename` 计算的全局名称唯一键。

`source_url`：仅 scheduled 使用的外部原始文档 URL；manual 必须为 `NULL`。

在 `IngestJobResponse` 中增加相应只读字段。客户端不得上传 `document_name_key`。

## 2. 文档名规范化

新增单一 helper，manual、scheduled、历史只读检查和迁移工具全部复用：

```text
Path(filename).stem
→ unicodedata.normalize("NFKC", value)
→ strip
→ 连续空白替换为一个空格
→ casefold
```

保留标点差异，不把 `行业报告`、`行业-报告`、`行业_报告` 强行合并。

## 3. 目录保存规则

删除 scheduled UUID 后缀行为，改为：

```text
manual    → raw/uploads/manual/<safe_filename>
scheduled → raw/uploads/scheduled/<safe_filename>
```

同名判断忽略扩展名，因此即使目标物理路径不同，也必须先受全局名称约束。

非 Markdown manual 文件转换后的 `.md` 保存在相同 manual 目录中，但仅作为 ingest 工作文件。

## 4. 重名占用

推荐数据库约束：

```sql
UNIQUE KEY uq_ingest_document_name_key (document_name_key)
```

流程：

1. 后端计算名称键。
2. 对未迁移的旧 scheduled 任务，以只读方式读取其 `original_filename` 并使用同一 helper 比较；命中则返回冲突，但不更新旧 scheduled 行。
3. 保存上传文件。
4. 创建 ingest job 并写入名称键。
5. 唯一键冲突时删除本次刚保存的文件并转换成 `IngestConflictError`。
6. 任务失败时将本任务 `document_name_key` 设为 `NULL`，释放名称。

新任务间的并发冲突必须由数据库唯一键裁决，不能只靠 `Path.exists()`。

返回：

```http
409 Conflict
```

```json
{"detail":"文档名称已存在，不能重复入库：report"}
```

## 5. API 约束

保留现有 `POST /api/ingest/jobs`，增加可选 multipart 字段：

```text
source_url
```

验证：

```text
trigger=manual    → source_url 必须为空
trigger=scheduled → source_url 必填，Pydantic 校验 http/https
```

manual Quartz UI 无需显式提交 `trigger`，继续使用默认 `manual`。

## 6. Scheduler 改造

扫描每个 `.md` 时：

1. 找同目录固定文件名 `readme.txt`。
2. 以 `utf-8-sig` 读取。
3. 使用严格行格式提取：

   ```regex
   (?im)^Source URL:\s*(https?://\S+)\s*$
   ```

4. 恰好一个有效 URL 才能提交。
5. 同目录多个 `.md`、缺少 URL、多个 URL 或非法协议均记录明确错误，不调用 ingest API。
6. multipart 同时发送 `file=A.md`、`trigger=scheduled`、`source_url=<URL>`。
7. API 返回 `409` 时记录重复跳过，不当作后端不可用。

不上传或保存 `readme.txt`、HTML、images、videos。

## 7. Source frontmatter 确定性修正

LLM 继续负责 Source 正文、标题和 slug，但后端写文件前必须修正 frontmatter。

manual：

```yaml
source_file: raw/uploads/manual/<original-file>
```

- 删除可能由 LLM 输出的 `source_url`。
- 即使 LLM 读取的是转换 `.md`，`source_file` 仍指向原始上传文件。

scheduled：

```yaml
source_url: "https://..."
```

- 删除可能由 LLM 输出的 `source_file`。
- 保存的 `A.md` 路径只保留在 `ingest_jobs.source_path`。

目标 `wiki/sources/<slug>.md` 已存在时必须失败，禁止静默覆盖。

## 8. Quartz PublishService 对接

Quartz emitter 需要读取 `llm-wiki-agent/raw/uploads/manual`。

在调用 Quartz build 时增加环境变量：

```text
WIKI_SOURCE_ROOT=<llm-wiki-agent 根目录>
```

不要把整个 `raw/` 复制进 wiki snapshot。原文件选择和复制由 Quartz emitter 完成。

## 9. 历史迁移：只处理 manual

迁移选择条件固定为：

```sql
trigger = 'manual'
```

迁移前 dry-run 输出：

- job id；
- 旧路径和新路径；
- 对应 Source 页面；
- 计划回填的名称键；
- 缺失文件、路径冲突和 manual 内部重名。

确认执行后：

1. 移动 manual 原文件到 `raw/uploads/manual/`。
2. 仅在可以确认归属时移动同 stem 转换/调试产物。
3. 更新 manual job 的 `source_path`。
4. 更新 manual Source 的 `source_file`。
5. 为非失败 manual job 回填 `document_name_key`。

禁止：

- 查询结果中混入 `trigger='scheduled'` 后继续执行；
- 移动旧 scheduled 文件；
- 修改旧 scheduled job、Source 或 URL；
- 自动删除重复历史页面。

迁移工具必须可重复 dry-run；写操作使用事务和明确文件回滚/恢复策略。

## 10. 测试

### 名称规则

- 同 stem 不同扩展名冲突。
- manual 与 scheduled 跨 trigger 冲突。
- NFKC、空白和大小写规则一致。
- 不同标点不误判。
- failed job 释放名称。
- 并发插入由唯一键拒绝第二个任务。

### Scheduler

- 正确读取一个 `Source URL`。
- BOM 文件可读。
- 缺失、多 URL、非法协议、多 Markdown 目录不提交。
- `409` 计为重复跳过。

### Source 页面

- manual 非 Markdown 转换后仍引用原始文件。
- scheduled 只写 `source_url`。
- 已存在 Source slug 不被覆盖。

### 迁移

- 只处理 manual fixture。
- scheduled fixture 在迁移前后字节、路径和数据库字段均不变。
- dry-run 不写文件和数据库。

### Publish

- build 环境包含正确 `WIKI_SOURCE_ROOT`。
- 其他 Quartz 构建参数和原子发布行为不变。

## 完成标准

- 项目 `.venv` 下单元测试通过。
- MySQL 升级测试和集成测试通过。
- API 文档明确两个字段和 `409`。
- `git status` 不包含运行生成的 raw/wiki/public 文件。
- 向 Quartz 线程交付最终 frontmatter、路径和环境变量契约。

