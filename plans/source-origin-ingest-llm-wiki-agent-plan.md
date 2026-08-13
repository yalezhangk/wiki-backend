# `llm-wiki-agent`：Raw 目录与 Source 来源契约执行计划

## 线程目标

同步知识资产仓库的目录和 Source frontmatter 规范，使 backend 和 Quartz 有唯一、明确的共享契约。

本线程以文档和验证为主，不重写 `tools/ingest.py`、转换工具或知识工作流。实际 `raw/` 和 `wiki/` 写入由 `wiki-backend` 业务流程完成。

## 目录契约

```text
raw/uploads/
├─ manual/
│  ├─ report.pdf
│  └─ report.md       # 可选转换工作文件
└─ scheduled/
   └─ A.md            # scheduler 提交的解析后 Markdown
```

职责：

- `manual/` 保存 UI 上传的原始文件及后端转换产物。
- `scheduled/` 保存新 scheduled ingest 提交的 `A.md`。
- scheduler 外部源目录中的 `readme.txt`、HTML、images、videos 不复制进本仓库。
- 目录由 backend 按需创建，不要求提交运行时资料。

## Source frontmatter 契约

### manual

```yaml
---
title: "Source Title"
type: source
tags: []
date: YYYY-MM-DD
source_file: raw/uploads/manual/report.pdf
---
```

`source_file` 指向上传原文件，不能指向 PDF/DOCX 转换后的 Markdown。

### scheduled

```yaml
---
title: "Source Title"
type: source
tags: []
date: YYYY-MM-DD
source_url: "https://example.com/article"
---
```

- 不写 `source_file`。
- `source_url` 来自同目录 `readme.txt` 的 `Source URL:` 行。
- URL 不参与文档重名判断。

## 文档名契约

本仓库不自行实现去重逻辑；全局重名由 backend 在创建任务前执行。

规则结果是：

```text
manual/report.pdf
scheduled/report.md
```

视为同名，只允许一个入库。

## 历史数据边界

本次只允许 backend 迁移历史 manual 数据：

- 旧 manual 原文件进入 `raw/uploads/manual/`；
- 对应 manual Source 更新 `source_file`。

明确禁止本线程或迁移工具：

- 移动旧 scheduled 文件；
- 修改旧 scheduled Source；
- 为旧 scheduled 补写 `source_url`；
- 自动删除或合并历史 Source；
- 清理当前未提交的 raw/wiki/graph 业务产物。

## 预期修改文件

```text
AGENTS.md
README.md                     # 仅当当前 README 描述 ingest/raw/source 契约时更新
```

不要修改：

```text
tools/ingest.py
tools/file_to_md.py
tools/llm_config.py
```

除非执行线程发现这些工具会直接破坏新目录或 frontmatter 契约；若发现，应停止并回到总线程讨论，不自行扩展范围。

## 验证

1. 文档明确 manual/scheduled 目录职责。
2. Source 模板明确 `source_file` 与 `source_url` 互斥。
3. `source_file` 示例指向原文件。
4. health/lint 不把 `source_url` 当作本地缺失文件。
5. 现有 `tools.lint`、health 或 frontmatter 校验若只接受 `source_file`，先报告兼容问题；只有确实需要时才做最小修改并补测试。
6. 运行本仓库规定的 lint/health 检查时必须使用项目 `.venv`。
7. `git diff` 不包含运行数据和无关知识页变化。

## 完成标准

- backend 与 Quartz 可以引用同一份明确的 Source 契约。
- 文档不把 scheduled `A.md` 描述为用户原始文档。
- 历史 scheduled 数据保持不变。
- 无无关工具重构或知识内容改写。

