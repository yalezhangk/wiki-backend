# Ingest 原始来源展示与全局重名控制总计划

## 状态

- 计划状态：待实施
- 涉及仓库：`wiki-backend`、`quartz`、`llm-wiki-agent`
- 实施方式：三个仓库分别开启执行线程，但必须遵守本计划定义的共享契约。
- 建议顺序：先 `wiki-backend`，再 `llm-wiki-agent` 契约同步，最后 `quartz`。

## 已确认需求

1. `llm-wiki-agent/raw/uploads/` 按 ingest 来源物理区分：

   ```text
   raw/uploads/manual/
   raw/uploads/scheduled/
   ```

2. manual ingest 的原始来源是 UI 上传的文件本身，例如 PDF、DOCX、Markdown。
3. scheduled ingest 的处理输入是已经解析好的 `A.md`；同目录 `readme.txt` 中的 `Source URL:` 是用户需要访问的原始来源。
4. Source 知识页必须显示原始来源入口：
   - manual：查看或下载上传的原始文件；
   - scheduled：跳转到 `Source URL`。
5. manual 与 scheduled 共用全局文档名称空间，同名文档不得再次创建 ingest 任务。
6. 不引入内容哈希去重、URL 去重、文档版本或 revision 系统。
7. 历史数据迁移只处理 `ingest_jobs.trigger='manual'`；历史 scheduled 文件、任务和 Source 页面保持不变。

## 两个新增字段的准确含义

### `document_name_key`

`document_name_key` 是后端内部使用的全局重名判断键，不是页面标题、Source slug 或展示给用户的文件名。

生成规则：

```text
original_filename
→ 取文件主名，去掉扩展名
→ Unicode NFKC
→ 去除首尾空白
→ 连续空白合并成一个空格
→ casefold
```

示例：

```text
Report.pdf   → report
report.docx  → report
REPORT.md    → report
Ｒｅｐｏｒｔ.pdf → report
```

用途：

- manual 与 scheduled 创建任务时均由后端计算，客户端不得自行提供。
- `queued`、`running`、`succeeded` 任务占用名称。
- `failed` 任务释放名称，允许修复后重新提交。
- 新任务通过数据库唯一约束防止并发重复创建。
- `source_url`、目录路径和文件扩展名均不参与重名判断。

### `source_url`

`source_url` 是 scheduled ingest 对应的外部原始文档 URL。

用途：

- scheduler 从 `A.md` 同目录的 `readme.txt` 中提取。
- 只接受 `http://` 或 `https://`。
- 保存到 `ingest_jobs.source_url`，用于审计和任务查询。
- ingest 成功时由后端确定性写入 `wiki/sources/*.md` frontmatter。
- Quartz Source 页面读取它并渲染“访问原文”外链。
- 不参与重名判断，也不用于判断两个 scheduled 文档是否相同。

取值约束：

```text
manual    → source_url = NULL
scheduled → source_url 必填
```

## 跨仓库数据契约

### manual Source

```yaml
---
title: "Report"
type: source
source_file: raw/uploads/manual/report.pdf
---
```

- `source_file` 必须指向 UI 上传的原始文件。
- 后端转换生成的 `.md` 只是 ingest 工作文件，不得替代 `source_file`。

### scheduled Source

```yaml
---
title: "Article"
type: source
source_url: "https://example.com/article"
---
```

- 不写 `source_file`。
- 不把 `A.html`、`images/`、`videos/` 复制到知识库或 Quartz release。
- 保存到 `raw/uploads/scheduled/` 的 `A.md` 仍通过 `ingest_jobs.source_path` 追踪，但不作为用户原始来源展示。

## 全局重名契约

以下情况全部返回 `409 Conflict` 或在 scheduler 中记为重复跳过：

```text
manual/report.pdf     + manual/report.docx
manual/report.pdf     + scheduled/report.md
scheduled/a/report.md + scheduled/b/report.md
```

失败任务不占用名称：

```text
report.pdf ingest 失败
→ document_name_key 释放
→ 允许重新上传 report.pdf
```

Source slug 还要保留独立的覆盖保护：目标 `wiki/sources/<slug>.md` 已存在时不得静默覆盖。

## 历史数据迁移边界

只迁移：

```sql
WHERE trigger = 'manual'
```

迁移内容：

1. 将 manual 原始文件从旧的 `raw/uploads/<file>` 移到 `raw/uploads/manual/<file>`。
2. 同步移动该 manual 任务产生的同 stem 转换文件或调试产物，但不得误动其他任务文件。
3. 更新 manual `ingest_jobs.source_path`。
4. 更新 manual Source frontmatter，使 `source_file` 指向原始上传文件的新路径。
5. 为非失败 manual 历史任务回填 `document_name_key`。

明确不做：

- 不移动旧 scheduled 文件到 `raw/uploads/scheduled/`。
- 不读取旧 scheduled 的 `readme.txt`。
- 不给旧 scheduled 任务回填 `source_url`。
- 不修改旧 scheduled Source frontmatter。
- 不删除或合并历史知识页。

旧 scheduled 名称仍需参与新任务的重名判断，但只能只读使用其 `original_filename` 进行规范化比较，不能借机改写其数据库记录或文件。

因此旧 scheduled Source 不会自动获得“访问原文”入口；只有新 scheduled ingest 使用新契约。以后若需要补齐，必须另立迁移任务。

## 原文件发布契约

Quartz 仍只构建 `llm-wiki-agent/wiki`，不得把整个 `raw/` 作为内容目录。

新增 Quartz 本地 emitter：

```text
wiki/sources/*.md 中被 source_file 引用的 manual 文件
→ 校验路径位于 raw/uploads/manual/
→ 复制到 public/source-files/manual/
```

映射：

```text
raw/uploads/manual/report.pdf
→ /source-files/manual/report.pdf
```

`source_url` 只渲染外链，不产生本地文件。

## 实施顺序和线程交接

### 线程一：`wiki-backend`

读取：`plans/source-origin-ingest-wiki-backend-plan.md`

完成并交付：

- 数据库和 API 契约；
- manual/scheduled 目录；
- 全局重名判断；
- scheduler URL 提取；
- Source frontmatter 确定性修正；
- manual-only 迁移工具；
- Quartz build 所需的 `WIKI_SOURCE_ROOT` 环境传递。

### 线程二：`llm-wiki-agent`

读取：`plans/source-origin-ingest-llm-wiki-agent-plan.md`

完成并交付：

- Source frontmatter 规范同步；
- `raw/uploads/manual` 与 `raw/uploads/scheduled` 的目录职责文档；
- 不修改 ingest 工具业务逻辑，不迁移旧 scheduled 数据。

### 线程三：`quartz`

读取：`plans/source-origin-ingest-quartz-plan.md`

依赖 backend 已确认的 frontmatter 和路径契约，完成：

- manual 文件选择性发布；
- Source 原文入口；
- `/library` 来源标识；
- URL 安全跳转和文件类型行为。

## 总体验收

1. manual 与 scheduled 新文件分别写入正确目录。
2. 相同主文件名跨扩展名、跨 trigger 均被拒绝。
3. failed 任务可以用同名文件重试。
4. manual Source 的 `source_file` 指向原始上传文件。
5. scheduled Source 的 `source_url` 与 `readme.txt` 完全一致。
6. PDF 可打开，Office 文件可下载，scheduled URL 可新标签跳转。
7. Quartz release 只包含 Source 实际引用的 manual 文件。
8. 旧 scheduled 数据没有发生文件、数据库或 Source 元数据迁移。
9. 迁移、构建或发布失败时保留原文件和上一版 Quartz release。

