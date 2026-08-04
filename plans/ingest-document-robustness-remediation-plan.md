# Ingest 文档健壮性修复计划

## 目标

只解决两点：

1. PDF 扫描件或 LLM 解析失败时，任务必须为 `failed`，不得写入“解析失败”页面或触发 Quartz publish。
2. 提高 PDF 为主、其他非 Markdown 文件为辅的转换前校验和失败可诊断性。

上传上限保持 10 MiB：`WIKI_BACKEND_INGEST_MAX_UPLOAD_BYTES=10485760`。

## 实施状态

- [x] 在调用 LLM 前检查转换结果：拒绝空内容、过短内容、过多控制字符或 `�`。
- [x] PDF 预检：加密为 `pdf_encrypted`、损坏为 `pdf_unreadable`；无文本层和符号伪文本先尝试本地 OCR。
- [x] Windows 使用不依赖 Docker 的 RapidOCR；其他系统保留 Marker 优先、RapidOCR fallback。
- [x] 原生文本 PDF 先用 MarkItDown，转换报错时再尝试 `pymupdf4llm`。
- [x] LLM 契约增加 `ingest_status` 和 `ingest_error`；只有
  `{"ingest_status":"succeeded","ingest_error":null}` 才能写 Wiki。
- [x] 失败短路：转换或 LLM 失败时不写 Wiki、不标记成功、不触发 publish。
- [x] 补充 fake converter / fake LLM 测试，覆盖扫描 PDF、低质量转换和 LLM 明确失败。

## 后续小步

1. 在 DGX ARM64 安装新增依赖后，用正常、扫描、损坏、加密 PDF 做真实样本验证。
2. 为 Office、HTML、表格和音频补各自的最小有效内容检查，保持同一失败短路。

## 验收

- 扫描/空白/乱码 PDF 不再出现 `succeeded` 或占位 Wiki 页面。
- LLM 返回 `ingest_status=failed` 时，job 为 `failed` 且 Wiki 未改动。
- 正常文本型 PDF 和 Markdown 仍能成功入库。
- Windows 完整测试通过；涉及新 PDF/OCR 依赖时先完成 DGX ARM64 验证。
