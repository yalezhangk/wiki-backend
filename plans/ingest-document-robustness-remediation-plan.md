# Ingest 文档解析健壮性修复计划

## 1. 目标与边界

本计划只解决两件事：

1. 文档转换失败或 LLM 无法提取有效知识时，任务不得标记为 `succeeded`，不得写入
   “解析失败”占位页面，也不得触发 Quartz publish。
2. 提高 PDF 为主、其他非 Markdown 格式为辅的解析成功率和可诊断性。

本期不处理多 worker、外部任务队列、分布式事务或大规模 UI 改造。上传限制保持
10 MiB：

```env
WIKI_BACKEND_INGEST_MAX_UPLOAD_BYTES=10485760
```

## 2. 当前根因

当前非 Markdown 流程是：

```text
文件 -> MarkItDown -> Markdown -> LLM JSON -> 写 Wiki -> succeeded
```

存在两个缺口：

- MarkItDown 没抛异常就被视为成功，没有检查输出是否为空、乱码、只有页码或控制字符。
- LLM 只要返回符合字段结构的 JSON，即使 `source_page` 写着“无法解析”，后端仍会写入
  Wiki 并标记成功。

扫描 PDF 没有文本层时，最容易触发该问题。

## 3. 修复方案

### 3.1 转换质量门禁

在 `_convert_to_markdown()` 之后、调用 LLM 之前执行确定性检查：

- 转换结果不能为空。
- 有效字符数不能过少。
- 异常控制字符和 Unicode replacement character `�` 比例不能过高。
- PDF 不能只有页码、图片占位符或解析器错误文本。
- 转换结果必须能按 UTF-8 读取。

建议新增：

```text
app/services/ingest_content_quality.py
```

失败类别保持简单：

```text
conversion_empty
conversion_low_quality
ocr_required
document_corrupted
document_encrypted
```

失败时固定执行：

```text
job=failed
不调用 LLM
不写 Wiki
不触发 publish
```

精确阈值由测试样本确定，避免误伤短 Markdown、代码、表格和中文文档。

### 3.2 LLM 明确返回成功或失败

调整 `app/prompts/ingest.md` 和 `IngestLLMResult`，要求返回：

```json
{
  "ingest_status": "succeeded",
  "ingest_error": null
}
```

无法提取有效知识时返回：

```json
{
  "ingest_status": "failed",
  "ingest_error": "未能从文档中提取有效正文"
}
```

后端遇到 `ingest_status=failed` 时必须在 `_write_ingest_result()` 前终止任务。

`ingest_status=succeeded` 仍需确认：

- `source_page` 不是空模板或失败占位页。
- Summary、Key Claims 等核心正文存在。
- `title`、`slug`、`index_entry`、`log_entry` 非空。

文件损坏、加密和无文本层由代码预检判断，不能交给 LLM 猜测。

### 3.3 PDF 解析增强

PDF 转换前检查：

- 能否打开并读取页数。
- 是否加密。
- 是否存在可提取文本页。
- 正文字符量是否与页数明显不匹配。

结果分类：

| 情况 | 处理 |
|---|---|
| 损坏 | `failed: document_corrupted` |
| 加密且无法读取 | `failed: document_encrypted` |
| 有页面但无文本层 | `failed: ocr_required`，或进入 OCR |
| 文本质量低 | 尝试备用解析器，仍失败则 `conversion_low_quality` |
| 文本质量合格 | 进入 LLM |

解析器策略：

```text
原生文本 PDF
  -> MarkItDown 或 PyMuPDF4LLM
  -> 质量检查
  -> 不合格时尝试备用解析器

扫描/复杂排版 PDF
  -> Marker 或 OCR
  -> 质量检查
```

可以参考 `llm-wiki-agent/tools/pdf2md.py`，但实现必须放在 `wiki-backend`，不得动态导入
或执行 sibling Python 源码。新依赖必须先在 DGX ARM64 验证。

首批修复可先准确返回 `ocr_required`，再单独接入 OCR；OCR 不可用时不得继续生成占位
Wiki 页面。

### 3.4 其他格式的最小增强

其他格式暂时继续使用 MarkItDown，但统一经过质量门禁：

- DOCX/PPTX：正文、表格或备注必须有有效内容；图片型文档明确提示需要 OCR。
- XLSX/XLS：至少存在有效 sheet 和单元格。
- HTML：静态 `body` 必须有正文；只有 JS 动态壳时失败。
- CSV/TSV/JSON/XML/YAML：编码可读、非空、基本结构合法。
- RTF：拒绝大量控制符进入 LLM。
- EPUB/IPYNB：最终正文非空。
- WAV/MP3：无可靠转写时失败；中文音频后续优先使用 DGX 本地 Whisper。

本期不为每种格式建立复杂类层级，只在 PDF 和音频确有独立依赖时拆分转换逻辑。

## 4. 实施顺序

### 阶段 1：先杜绝假成功

- 新增 `ingest_content_quality.py`。
- 转换后、LLM 前执行质量门禁。
- 增加 `ingest_status/ingest_error` 契约。
- 转换或 LLM 失败时不写 Wiki、不 publish。

验收：扫描、空白和乱码 PDF 不再生成“解析失败”Wiki 页面，job 为 `failed`。

### 阶段 2：增强 PDF

- 增加损坏、加密、页数和文本层预检。
- DGX 验证并接入 PyMuPDF4LLM 备用路径。
- 扫描件先返回 `ocr_required`，OCR 后续独立接入。

验收：正常、扫描、损坏、加密 PDF 能正确分类，备用解析器输出仍经过质量门禁。

### 阶段 3：覆盖其他格式

- 为 Office、HTML、文本、EPUB/IPYNB 和音频补最小专项检查。
- 图片型文档和不可用音频转写明确失败。

验收：所有支持格式要么产生有效正文，要么明确失败，不再出现空内容成功。

## 5. 计划修改文件

| 文件 | 变更 |
|---|---|
| `app/services/ingest_content_quality.py` | 新增转换质量检查 |
| `app/services/ingest_service.py` | 质量门禁、失败短路、PDF 预检和 fallback |
| `app/schemas/ingest.py` | `ingest_status/ingest_error` |
| `app/prompts/ingest.md` | 明确 LLM 成功和失败语义 |
| `requirements.txt` | 仅加入经 DGX 验证的 PDF/OCR 依赖 |
| `tests/test_ingest_content_quality.py` | 新增质量规则测试 |
| `tests/test_ingest_service.py` | 增加失败短路和 PDF 分流测试 |
| `tests/test_ingest_api.py` | 验证 failed 状态和错误说明 |
| `tests/fixtures/ingest/` | 小型非敏感多格式样本 |
| `README.md` | 更新格式能力和失败语义 |

## 6. 必测场景

| 样本 | 预期结果 |
|---|---|
| 正常 Markdown、原生文本 PDF | succeeded |
| 扫描 PDF、无 OCR | failed / `ocr_required` |
| 损坏或加密 PDF | failed，正确错误类别 |
| 空白或乱码转换结果 | failed，LLM 未调用 |
| LLM 返回 `ingest_status=failed` | failed，Wiki 未写，publish 未调用 |
| 正常 DOCX/PPTX/XLSX/HTML | succeeded |
| 图片型 Office 文档、无有效音频转写 | failed，不允许占位成功 |

默认测试使用 fake storage、fake converter 和 fake LLM，不调用真实模型或写真实 Wiki。

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests
```

## 7. 完成标准

1. 转换不合格时不调用 LLM。
2. LLM 明确失败时 job 为 `failed`，不写 Wiki、不 publish。
3. 正常、扫描、损坏、加密 PDF 能正确分类。
4. PDF 备用解析器能提高成功率，且输出统一经过质量门禁。
5. 其他支持格式不再出现空内容 `succeeded`。
6. Windows 完整测试通过；新增解析/OCR 依赖完成 DGX ARM64 验证。
