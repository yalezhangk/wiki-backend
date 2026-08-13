# `quartz`：Source 原文件与 URL 展示执行计划

## 线程目标

基于 backend 已确定的 Source frontmatter：

- 发布 manual Source 实际引用的原始文件；
- scheduled Source 显示外部原始 URL；
- 在 `/library → Source` 中提供原文入口；
- 不把整个 `llm-wiki-agent/raw` 加入 Quartz 内容输入。

本线程不得修改 backend 数据库和 scheduler，不得修改 `llm-wiki-agent` 运行数据。

## 输入契约

manual：

```yaml
source_file: raw/uploads/manual/report.pdf
```

scheduled：

```yaml
source_url: "https://example.com/article"
```

旧 Source 可能两者都没有；UI 必须兼容并隐藏原文入口。

历史 scheduled 不在本次迁移范围内；旧的平铺 `source_file` 不得被 emitter 当成新 manual 文件发布。

## 主要改动文件

```text
.local-plugins/source-files/*                # 新增本地 emitter
.local-plugins/knowledge-ui/src/knowledge.ts
.local-plugins/knowledge-ui/src/knowledge.test.ts
.local-plugins/knowledge-ui/src/components/AppNavigation.tsx
.local-plugins/knowledge-ui/src/components/LibraryPage.tsx
.local-plugins/knowledge-ui/src/components/*SourceReference*
.local-plugins/chats/src/types.ts
quartz.config.yaml
quartz/styles/custom.scss
README.md
AGENTS.md
```

## 1. Source Files emitter

新增本地 emitter，只处理 frontmatter 中符合以下前缀的文件：

```text
raw/uploads/manual/
```

构建时的实际根目录：

```text
process.env.WIKI_SOURCE_ROOT
```

若环境变量未设置，允许在直接构建真实 `.../llm-wiki-agent/wiki` 时从输入目录父级推导；自动发布使用临时 snapshot，必须由 backend 显式设置环境变量。

输出映射：

```text
raw/uploads/manual/report.pdf
→ public/source-files/manual/report.pdf
→ /source-files/manual/report.pdf
```

安全规则：

- 只扫描已发布 `sources/*.md` 的 `source_file`。
- 必须为相对路径并位于 `raw/uploads/manual/`。
- `resolve/realpath` 后仍必须位于 `WIKI_SOURCE_ROOT/raw/uploads/manual`。
- 拒绝 `..`、绝对路径、符号链接逃逸、目录和缺失文件。
- 同一文件只复制一次。
- 发现无效引用时构建失败，不生成不完整 release。
- scheduled `source_url` 不产生静态文件。

不得修改 Quartz 核心 Assets emitter，也不得把 `raw/` 加入 `-d` 输入。

## 2. KnowledgeObject 元数据

扩展 knowledge-ui：

```ts
sourceFile: string | null
sourceUrl: string | null
```

解析规则：

- 仅 `type=source` 使用。
- `source_file` 必须是非空字符串且前缀为 `raw/uploads/manual/`。
- `source_url` 必须是 `http/https`。
- 两者同时出现时标记元数据无效，不渲染任意一个入口，避免静默选错。

辅助函数统一生成：

- 原文 href；
- 文件扩展名标签；
- URL hostname；
- 按钮文案和打开方式。

## 3. Source 详情页

新增 SourceReference 组件或等效的 knowledge-ui 局部组件，只在 Source 页面显示。

manual 示例：

```text
原始来源
report.pdf · PDF · 人工上传
[查看原文]
```

scheduled 示例：

```text
原始来源
mp.weixin.qq.com · 定时同步
[访问原文]
```

同时在 AppNavigation 当前 Source 操作区增加“查看原文/访问原文”。

URL 外链必须使用：

```html
target="_blank"
rel="noopener noreferrer"
```

## 4. `/library` Source 行

Source 行增加轻量元数据：

```text
[PDF] report.pdf
[DOCX] equipment.docx
[URL] mp.weixin.qq.com
```

整行主链接仍进入 Source 知识页，不直接下载文件。

搜索文本可以包含文件名或 hostname，方便用户查找；不得把 `raw/uploads/...` 内部路径直接当主文案展示。

## 5. 文件行为

第一版不开发 Office 在线预览：

| 类型 | 行为 |
|---|---|
| PDF | 新标签打开 |
| 图片 | 新标签打开 |
| Markdown/TXT | 新标签文本显示或下载 |
| DOCX/PPTX/XLSX | 下载原文件 |
| HTML | 默认下载，不在主站同源执行 |
| URL | 新标签跳转 |

如需 Nginx 配置：

- `/source-files/` 作为静态路径，不走 `/api`；
- PDF 支持 Range 请求；
- HTML/Office 默认 attachment；
- 添加 `X-Content-Type-Options: nosniff`；
- ECS 可缓存静态文件，但不得改变 `/api` 的 BYPASS 规则。

## 6. Chats/Ingest UI

manual 上传 API 调用保持现状：只上传原始 `File` 和 `auto_convert=true`，不提交 `trigger` 或 `source_url`。

更新 TypeScript `IngestJobResponse` 以兼容 backend 新字段，并确保 backend `409` 的具体错误可以显示在文档入库页。

不得在浏览器端实现最终重名判断；后端是唯一权威。

## 7. 构建流程

修改 knowledge-ui 或 source-files 插件后必须分别构建其 `dist`，然后执行完整 Quartz build。

生产直接构建命令增加：

```bash
WIKI_SOURCE_ROOT=/home/dgx/Projects/knowledge_base_mkt/llm-wiki-agent \
CHAT_PROXY_URL=/api \
npx quartz build \
  -d /home/dgx/Projects/knowledge_base_mkt/llm-wiki-agent/wiki
```

## 8. 测试

- knowledge model 正确区分 manual file、scheduled URL 和无来源的旧 Source。
- `source_file` 和 `source_url` 同时存在时不渲染错误入口。
- `/library` 显示正确类型或 hostname。
- Source 顶部操作和来源卡片 href 正确。
- URL 协议过滤、`target` 和 `rel` 正确。
- emitter 只复制被引用的 manual 文件。
- 路径穿越、绝对路径、符号链接、缺失文件导致构建失败。
- 旧 scheduled 平铺路径不被复制。
- 完整构建产出 `index.html`、`ingest.html`、`chats.html`、`static/contentIndex.json` 和引用的 `/source-files/` 文件。
- 构建 HTML 中没有 `/quartz/` 错误前缀。

## 完成标准

- 本地插件测试和构建通过。
- 完整 Quartz build 通过。
- manual PDF/Markdown/Office 行为符合表格约定。
- scheduled URL 可以安全跳转。
- 未引用 raw 文件不出现在 release。
- 不手工修改 `public/`，不提交运行生成的 `public`。

