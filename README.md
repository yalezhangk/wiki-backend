# wiki-backend

`wiki-backend` 是 LLM Wiki 的 HTTP API 服务。它把 `llm-wiki-agent` 的 query、ingest 和 Wiki 写入能力封装为 FastAPI 接口，同时使用 MySQL 保存聊天、消息和 ingest 任务状态。

## 职责边界

- `llm-wiki-agent`：供 Codex、Claude Code 等 Coding Agent 直接运行的知识库 Agent/Skill，并保存 Wiki 文件。
- `wiki-backend`：独立实现 HTTP query、ingest、聊天编排、任务状态、数据库持久化和 synthesis 写入。
- `quartz`：读取 Wiki Markdown，构建静态 UI，并通过同源 `/api` 调用本服务。

`wiki-backend` 不直接提供 Quartz 静态页面；它会在 ingest 或 synthesis 成功后编排 Quartz 构建，并由 DGX Nginx 继续从 `quartz/public/` 提供静态页面。

后端只共享 `llm-wiki-agent` 的 `wiki/`、`raw/` 和 `graph/` 数据，不导入其 Python 源码。LLM 调用配置位于 `app/llm_config.py`，query 和 ingest 使用的 Agent Prompt 固化在 `app/prompts/agent_instructions.md`。该 Prompt 只同步自 `llm-wiki-agent/AGENTS.md`，不使用 `CLAUDE.md`。

## 当前部署拓扑

```text
局域网或公网浏览器
  -> DGX Nginx :8080
     -> /api/*
     -> wiki-backend 127.0.0.1:8081

公网入口额外经过：
ECS Nginx :8080
  -> ECS 127.0.0.1:18080
  -> FRP
  -> DGX Nginx 127.0.0.1:8080
```

生产环境只保留 ECS `18080` 到 DGX Nginx `8080` 的一条业务隧道。后端监听 DGX 回环地址 `127.0.0.1:8081`，不直接暴露到局域网或公网，也不使用已删除的 ECS `18081`。

## API

- `GET /api/health`：进程存活检查，不验证 MySQL 或 LLM。
- `POST /api/query`：无状态单轮知识库问答。
- `GET /api/chats`：列出会话。
- `POST /api/chats`：创建会话。
- `PATCH /api/chats/{chat_id}`：更新会话。
- `GET /api/chats/{chat_id}/messages`：读取会话消息。
- `POST /api/chats/{chat_id}/messages`：发送消息并生成回答。
- `POST /api/ingest/jobs`：提交文档入库任务。
- `GET /api/ingest/jobs`：列出入库任务。
- `GET /api/ingest/jobs/{job_id}`：读取任务详情。
- `POST /api/synthesis`：将已持久化的助手回答保存为 Wiki synthesis。
- `GET /api/publish/status`：读取待发布变更、当前构建和最近成功发布。
- `POST /api/publish/jobs`：立即构建并发布当前 Wiki。
- `GET /api/publish/jobs*`：读取发布任务历史与详情。
- `POST /api/maintenance/jobs`：创建单项 Wiki 维护任务，返回 `202 Accepted` 仅表示入队。
- `GET /api/maintenance/jobs*`：读取维护任务的审计状态与结构化摘要，不返回原始报告正文。
- `POST /api/maintenance/workflows/quality`：创建 `health → graph → lint` 依赖工作流。
- `GET /api/quality/latest`：只读最近质量报告快照；不会运行巡检、调用 LLM、写 Wiki 或发布 Quartz。

`/api/maintenance/*` 是管理接口。Health、显式 WikiLink Graph 和 Lint 的确定性检查均在单 worker 中执行；Graph 的 `infer_relations` 默认 `true`，会调用后端自有 LLM 配置并可能产生费用，`save_report` 默认 `true`；质量工作流中的 Graph 使用相同默认值。Lint 的导航孤儿只在页面没有任何可解析的 WikiLink 或本地 Markdown 入链时告警；只缺 WikiLink、但可由 Markdown 索引访问的页面记录为 `graph_orphan` 信息项。`health-report.md` 等生成报告不参与 lint 页面检查。Lint 的 `semantic_mode=agent_compat` 会复刻 Agent 的前 20 页、每页 1500 字符 Markdown 语义检查。`delta`、`risk`、`full` 与 `selected` 是后端扩展语义模式，不属于 Agent 兼容性承诺。Graph 推断和 Lint 语义阶段仅在相应选项开启时调用后端自有 LLM 配置。任一可选 LLM 阶段失败时，确定性产物仍保留，任务以 `succeeded + partial` 标记，不能将语义结论当作可用结果。DGX 与 ECS Nginx 必须在 HTTPS、认证和限流配置完成前拒绝该前缀的公网访问。

维护、质量统计和问答共用同一知识页范围：`wiki/overview.md` 与 `wiki/sources/`、`wiki/entities/`、`wiki/concepts/`、`wiki/syntheses/` 下的 Markdown。`wiki/index.md` 只作为导航与索引同步输入，`wiki/log.md` 只用于日志覆盖检查；二者都不作为图节点、Lint 语义上下文或问答引用。根目录的 `wiki/health-report.md`、`wiki/lint-report.md` 以及 `graph/graph-report.md` 是运行产物，不进入知识页扫描或问答。`GET /api/quality/latest` 仅为质量快照定向读取这些报告。

质量快照以报告文件修改时间判断新鲜度。可通过 `WIKI_BACKEND_QUALITY_STALE_AFTER_HOURS` 调整阈值，默认 `168` 小时；报告缺失、过期或解析失败均以领域状态返回 `200`，只有 Wiki 根目录不可访问时返回 `503`。

服务启动后可查看 FastAPI 文档：

```text
http://127.0.0.1:8081/docs
```

生产浏览器应通过同源入口访问，例如 `<SITE_ORIGIN>/api/health`，而不是直接请求 `8081`。

### 当前响应契约基线

- `chats.id`、`chat_messages.chat_id`、`ingest_jobs.job_id`、`publish_jobs.job_id` 与发布状态中的 `job_id` 均为正整数 JSON number。此前 UUID 路径和 UUID 请求体不再兼容，会返回 `422`。
- 数据库业务时间字段精确到秒，当前序列化为不带 `Z` 或时区偏移的 ISO 8601 字符串。部署本变更后新建或更新的记录按北京时间（`Asia/Shanghai`）写入，例如 `2026-07-22T18:01:08`；既有记录保留原始 UTC 数值，不做迁移。
- `source_path` 相对于 `WIKI_AGENT_REPO_PATH` 指向的 agent 仓库根目录，例如 `raw/uploads/report.md`。
- Ingest 响应的 `trigger` 表示任务来源：`manual` 为 UI/API 人工上传，`scheduled` 为 DGX 定时 Markdown 同步；旧记录兼容为 `manual`。
- `relevant_pages`、`created_pages`、`updated_pages`、`synthesis_path` 和 Synthesis 响应中的 `path` 都是相对于 `llm-wiki-agent/wiki` 的路径，统一使用 `/` 分隔符。
- `sources` 是本次回答可引用的 Wiki 根目录相对路径，按检索顺序稳定去重，统一使用 `/` 分隔符并保留 `.md` 后缀。`POST /api/query` 正文中的 `[n]` 对应 `sources[n - 1]`；聊天回答使用实际 Wiki 页面链接。`relevant_pages` 仍表示检索到的扩展阅读上下文。
- `POST /api/query` 保持无状态，不会隐式创建 Chat。`POST /api/ingest/jobs` 返回 `202 Accepted` 仅表示任务已入队，`succeeded` 也不表示 Quartz 已发布。
- 成功的 ingest 与 synthesis 响应可额外包含 `publication`：`pending`、`running`、`published` 或 `failed`。这不改变原有 ingest 状态机。

以上格式是兼容现有 Quartz 客户端的 Phase B0 基线。后续只以新增字段方式增强响应，不删除现有 `sources`、`relevant_pages` 或 Ingest 字段。

### Ingest 阶段与进度

Ingest 响应在保留 `status` 的同时提供 `stage`、`progress_percent` 和 `updated_at`。阶段与当前真实执行边界对应：

| `stage` | `progress_percent` | 含义 |
|---|---:|---|
| `uploaded` | 0 | 文件已保存且任务已入队 |
| `converting` | 10 | 非 Markdown 文件正在转换；Markdown 任务会跳过 |
| `extracting` | 35 | 正在读取转换结果并调用 LLM 提取结构化知识 |
| `writing_wiki` | 65 | 正在写入 Wiki 页面、索引和日志 |
| `validating` | 85 | 正在检查断链和未索引页面 |
| `completed` | 100 | 知识文件写入和校验已完成 |

失败任务保留失败前最后一个 `stage` 和 `progress_percent`。这些百分比是离散阶段值，不表示阶段内部完成度，也不包含 Quartz build/publish 进度。PDF 会先检查加密和损坏；没有文本层或符号伪文本时，所有系统默认使用本地 `RapidOCR + ONNX Runtime`，不依赖 Docker。原生文本 PDF 优先使用 MarkItDown，失败时使用 PyMuPDF 提取文本。`marker-pdf` 是可选的高保真 OCR，只有安装 `requirements-marker.txt` 并设置 `WIKI_BACKEND_INGEST_ENABLE_MARKER_OCR=true` 后才会尝试；Marker 超时、报错、无输出或低质量输出都会回退 RapidOCR。若 OCR 未安装、超时或仍无法提取正文，任务会以 `ocr_unavailable` 或 `ocr_failed` 结束，不会写入 Wiki 或触发 Quartz 发布；LLM 必须显式返回 `ingest_status: "succeeded"`，否则任务不会成功。

上传文件采用分块读取，默认最大为 10 MiB，可通过
`WIKI_BACKEND_INGEST_MAX_UPLOAD_BYTES` 调整。服务端会校验声明的 MIME 类型；PDF、Office、EPUB、XLS、RTF、WAV 和 MP3 等格式还会检查文件签名或容器结构。校验失败的临时上传文件会被删除，不创建 Ingest 任务。

`created_pages` 和 `updated_pages` 根据本次写入前目标 Wiki 文件是否存在区分；重新入库并覆盖已有 Entity 或 Concept 时会进入 `updated_pages`。

### 结构化引用

`POST /api/query`、`POST /api/chats/{chat_id}/messages` 和会话历史响应在保留 `sources`、`relevant_pages` 的同时返回 `citations`：

```json
{
  "path": "entities/MDC4.md",
  "title": "MDC4",
  "kind": "entity",
  "excerpt": null,
  "relevance": null
}
```

- `path` 是经过越界检查的 Wiki 根目录相对路径。
- `title` 依次来自 frontmatter、首个一级标题和文件名。
- `kind` 支持 `source`、`entity`、`concept`、`synthesis`、`page`；未知类型回退为 `page`。
- 当前检索层没有真实命中片段或相关度分数，因此 `excerpt`、`relevance` 返回 `null`，不生成推测值。
- `citations` 与 `sources` 按相同顺序提供可展示的路径、标题和类型；`POST /api/query` 回答正文可使用 `[n]` 定位到第 `n` 条来源，聊天回答使用 `[[path|title]]` 实际页面链接。检索上下文仍单独保存在 `relevant_pages`。
- Chat 引用以 JSON 保存到 MySQL，刷新历史消息后仍可恢复；迁移前的旧消息安全回填为空列表。

### synthesis 请求示例

```json
{
  "chat_id": 42,
  "assistant_message_id": 42,
  "title": "可选标题"
}
```

前端只提交消息身份，不提交答案正文。成功后，服务会在配置的 `llm-wiki-agent` 仓库内写入 `wiki/syntheses/*.md`，更新相关 Wiki 索引和日志，并记录消息的 synthesis 状态。

## 环境要求

- Python 3.10+
- 项目内虚拟环境 `.venv`
- MySQL 8+，推荐 InnoDB 和 `utf8mb4`
- `WIKI_AGENT_REPO_PATH` 用于定位共享的 `wiki/`、`raw/` 和 `graph/` 数据目录，不用于导入 agent 源码
- 最终运行环境：NVIDIA DGX Spark，Ubuntu ARM64

当前优先使用 DGX 宿主机原生 `uv + .venv`，不以 Docker 作为默认部署路径。

## 配置

复制 `.env.example` 的配置项到仅在服务器维护的 `.env`：

```env
WIKI_AGENT_REPO_PATH=../llm-wiki-agent
WIKI_BACKEND_MYSQL_HOST=127.0.0.1
WIKI_BACKEND_MYSQL_PORT=3306
WIKI_BACKEND_MYSQL_USER=wiki_backend_app
WIKI_BACKEND_MYSQL_PASSWORD=replace-with-a-strong-password
WIKI_BACKEND_MYSQL_DATABASE=wiki_backend
WIKI_BACKEND_DEFAULT_CHAT_TITLE=新对话
WIKI_BACKEND_CHAT_HISTORY_LIMIT=6
WIKI_BACKEND_INGEST_MAX_UPLOAD_BYTES=10485760
WIKI_BACKEND_INGEST_LLM_MAX_TOKENS=8192
WIKI_BACKEND_INGEST_ENABLE_MARKER_OCR=false
# 仅在启用 DGX 定时 Markdown 同步时配置；不要提交真实目录。
WIKI_BACKEND_SCHEDULED_INGEST_ROOT=/path/to/source-directory
WIKI_BACKEND_SCHEDULED_INGEST_API_URL=http://127.0.0.1:8081
WIKI_BACKEND_SCHEDULED_INGEST_POLL_SECONDS=2
WIKI_BACKEND_SCHEDULED_INGEST_POLL_TIMEOUT_SECONDS=7200
WIKI_BACKEND_LLM_PROVIDER=deepseek
WIKI_BACKEND_LLM_FAST_MODEL=deepseek-v4-flash
WIKI_BACKEND_LLM_MAIN_MODEL=deepseek-v4-pro
WIKI_BACKEND_LLM_API_KEY=
WIKI_BACKEND_LLM_API_BASE=
```

真实 `.env` 不提交 Git。DGX 上使用 Linux 路径，不要写入 Windows 反斜杠路径。模型密钥只写入服务器 `.env`；不要把 `llm-wiki-agent/tools/llm_config.py` 中的本地配置或密钥复制到本项目。

LLM 只使用一套连接配置：`WIKI_BACKEND_LLM_PROVIDER`、`WIKI_BACKEND_LLM_API_KEY` 和 `WIKI_BACKEND_LLM_API_BASE`。`FAST_MODEL` 仅用于页面选择、图关系推断等轻量任务，`MAIN_MODEL` 用于问答与 ingest；两者始终走同一个模型服务。对 `ollama` 或 `ollama_chat`，后端不会发送 API key。

### 切换为 DGX 同机 Ollama

LiteLLM 使用 `ollama_chat` 时会请求 Ollama 的 `/api/chat`，因此与你已验证的 `curl` 接口一致。后端与 Ollama 在同一台 DGX 上运行时使用 loopback 地址，不使用局域网地址或 `/v1` 后缀：

```env
WIKI_BACKEND_LLM_PROVIDER=ollama_chat
WIKI_BACKEND_LLM_FAST_MODEL=qwen3.6:27b
WIKI_BACKEND_LLM_MAIN_MODEL=qwen3.6:27b
WIKI_BACKEND_LLM_API_KEY=
WIKI_BACKEND_LLM_API_BASE=http://127.0.0.1:11434
WIKI_BACKEND_LLM_FAST_MAX_TOKENS=5120
WIKI_BACKEND_LLM_MAIN_MAX_TOKENS=8192
```

编辑 DGX 的私有 `.env` 后重启 `wiki-backend`；先以 `GET /api/health` 确认进程，再执行一次只读 `POST /api/query` 验证回答。不要把 Ollama `11434`、后端 `8081` 或真实 `.env` 暴露到公网。

`WIKI_BACKEND_INGEST_LLM_MAX_TOKENS` 仅控制 Ingest 主模型的单次输出预算，默认保持兼容的 `16384`。提高该值前必须先确认所用模型支持对应上限；模型返回 `finish_reason=length` 时，任务会以可识别的截断错误失败，不会用相同 Prompt 盲目重试。

## MySQL 初始化

示例：

```sql
CREATE DATABASE wiki_backend
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

CREATE USER 'wiki_backend_app'@'127.0.0.1'
  IDENTIFIED BY 'replace-with-a-strong-password';

GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, REFERENCES
  ON wiki_backend.* TO 'wiki_backend_app'@'127.0.0.1';

FLUSH PRIVILEGES;
```

如果应用通过 `127.0.0.1` 建立 TCP 连接，授权 host 也应包含 `127.0.0.1`。是否还需创建 `localhost` 用户取决于实际连接方式，不必无条件重复授权。

应用启动时会初始化：

- `chats`
- `chat_messages`
- `ingest_jobs`
- `publish_jobs`
- `publish_changes`
- `maintenance_jobs`
- `maintenance_page_state`
- `maintenance_findings`
- 必要索引

MySQL 保存业务元数据，不保存 Wiki Markdown 正文。

### UUID 主键历史迁移

现有数据库若仍使用 UUID 主键，必须在维护窗口完成迁移。先停止 FastAPI、ingest worker 和 publish worker，并完成可恢复的 MySQL 备份；迁移工具不会在服务启动时自动执行。

```powershell
.venv\Scripts\python.exe -m tools.migrate_uuid_primary_keys migrate --confirm
```

DGX：

```bash
.venv/bin/python -m tools.migrate_uuid_primary_keys migrate --confirm
```

工具会复制数据到数字 ID 影子表，重写 `chat_messages.chat_id`、`publish_changes.publish_job_id` 以及 ingest 类型的 `publish_changes.source_id`，验证后切换表名。原 UUID 表会保留为 `*_uuid_backup`，请先完成 `/api/health`、`/api/chats`、ingest 和 publish smoke test；确认无误后再显式清理备份表：

```powershell
.venv\Scripts\python.exe -m tools.migrate_uuid_primary_keys finalize --confirm
```

DGX：

```bash
.venv/bin/python -m tools.migrate_uuid_primary_keys finalize --confirm
```

历史 `publish_jobs.release_id` 保持原值，因此已存在的 UUID 命名 Quartz release 目录仍可追溯；新发布任务使用数字目录名。

## 安装与启动

### Windows 开发机

所有 Python 命令必须使用项目虚拟环境：

```powershell
cd C:\job_docs\knowledge_base\mvc_sample\wiki-backend

# 若 .venv 尚不存在，先用已安装的 Python 3.10+ 创建一次
python -m venv .venv

.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8081 --reload
```

创建 `.venv` 是唯一允许临时调用系统 Python 的场景；创建后，安装、启动和测试都必须使用 `.venv\Scripts\python.exe`。

### DGX Ubuntu ARM64

```bash
cd /home/dgx/Projects/knowledge_base_mkt/wiki-backend

uv venv --python 3.12
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m pip check
.venv/bin/python -c "import pdfplumber, pymupdf; from rapidocr import RapidOCR"

.venv/bin/python -m uvicorn app.main:app \
  --host 127.0.0.1 \
  --port 8081
```

当前显式 Uvicorn 命令和模块内置入口都只监听 `127.0.0.1:8081`，不会监听 `0.0.0.0`，因此不能再通过 DGX 的局域网地址直接访问 `8081`。生产环境仍不要直接执行 `.venv/bin/python -m app.main`，因为模块内置入口会启用 reload；长期运行应使用上面的显式 Uvicorn 命令，或使用等价的 systemd 服务配置。

默认依赖不安装 `marker-pdf` 或 `pymupdf4llm`：文本 PDF 使用 MarkItDown/PyMuPDF，扫描 PDF 使用 RapidOCR。RapidOCR 首次处理扫描件会下载 OCR 模型，因此应在部署阶段上传一份非敏感扫描 PDF 预热一次；成功前不要启动长期运行服务。只有确实需要更复杂版面还原、且已验证 Docker、GPU、模型下载和输出质量时，才安装可选 Marker 并在服务器 `.env` 中启用：

```bash
uv pip install --python .venv/bin/python -r requirements-marker.txt
# .env 中设置：WIKI_BACKEND_INGEST_ENABLE_MARKER_OCR=true
```

即使启用 Marker，超时、错误、没有 Markdown 或输出质量不合格时仍会自动回退 RapidOCR。依赖更新后应先在 DGX ARM64 上重新安装和验证，再重启长期运行进程。

### 使用 systemd 后台运行

生产环境使用 `systemd` 管理 Uvicorn 进程：

创建 `/etc/systemd/system/wiki-backend.service`：

```bash
sudo nano /etc/systemd/system/wiki-backend.service
```

写入以下内容：

```ini
[Unit]
Description=Wiki Backend FastAPI Service
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=dgx
Group=dgx

WorkingDirectory=/home/dgx/Projects/knowledge_base_mkt/wiki-backend
Environment=HOME=/home/dgx
Environment=PYTHONUNBUFFERED=1

ExecStart=/home/dgx/Projects/knowledge_base_mkt/wiki-backend/.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8081

Restart=on-failure
RestartSec=5s
TimeoutStopSec=120s
KillSignal=SIGTERM

[Install]
WantedBy=multi-user.target
```

`WorkingDirectory` 必须指向项目根目录，使应用能够读取项目内的 `.env`。`ExecStart` 必须直接使用项目虚拟环境，并保持监听 `127.0.0.1:8081`。

确认旧进程退出、`8081` 已释放后，验证并启用服务：

```bash
sudo systemd-analyze verify /etc/systemd/system/wiki-backend.service
sudo systemctl daemon-reload
sudo systemctl enable --now wiki-backend.service
```

检查运行和开机自启状态：

```bash
sudo systemctl status wiki-backend.service --no-pager --full
sudo systemctl is-active wiki-backend.service
sudo systemctl is-enabled wiki-backend.service
```

常用维护命令：

```bash
# 启动
sudo systemctl start wiki-backend.service

# 停止
sudo systemctl stop wiki-backend.service

# 重启
sudo systemctl restart wiki-backend.service

# 查看完整状态
sudo systemctl status wiki-backend.service --no-pager --full

# 查看最近 200 行 systemd 日志
sudo journalctl -u wiki-backend.service -n 200 --no-pager

# 实时查看 systemd 日志
sudo journalctl -u wiki-backend.service -f

# 实时查看应用轮转日志
tail -f /home/dgx/Logs/knowledge_base_mkt/wiki-backend/wiki-backend.log

# 取消开机自启
sudo systemctl disable wiki-backend.service
```

代码或依赖更新后的推荐流程：

```bash
cd /home/dgx/Projects/knowledge_base_mkt/wiki-backend

sudo systemctl stop wiki-backend.service
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m unittest discover -s tests
sudo systemctl start wiki-backend.service

sudo systemctl status wiki-backend.service --no-pager --full
curl --fail --silent --show-error http://127.0.0.1:8081/api/health
curl --fail --silent --show-error http://127.0.0.1:8081/api/chats > /dev/null
curl --fail --silent --show-error http://127.0.0.1:8080/api/health
curl --fail --silent --show-error http://127.0.0.1:8080/api/chats > /dev/null
```

如果只是没有依赖变化的小型代码更新，在测试通过后可以直接执行 `sudo systemctl restart wiki-backend.service`。`/api/health` 只验证 FastAPI 进程存活，仍需通过 `/api/chats` 检查 MySQL 路径。

### 每日增量 Markdown 入库

配置 `WIKI_BACKEND_SCHEDULED_INGEST_ROOT` 后，`.venv/bin/python -m app.scheduled_ingest` 会递归扫描
该目录的普通 `.md` 文件，不跟随符号链接。首次运行处理现有文件；后续仅处理此前未记录的路径和文件
身份。文件在快照期间仍发生变化时会留待下次运行。空 Markdown 会进入现有 Ingest 校验流程，并在两次
失败后成为最终失败记录，而不会每天无限延后。每个失败文件只尝试两次，第二次失败后保存最终失败审计并
不再每日重试。同步命令只经 `http://127.0.0.1:8081` 调用现有 Ingest API，成功任务仍由后端自动排队
Quartz 发布。

如果同步进程异常中断，下一次运行会先核对遗留记录：已经终态的任务会归并结果；无法确认请求结果、
关联任务丢失或前次任务仍未终态的记录会标记为最终失败并写入错误日志，不会自动重传造成重复 Wiki 写入。

将仓库中的示例复制为 systemd 单元，并把占位符替换为 DGX 实际运行用户和项目目录：

```bash
sudo cp docs/wiki-backend-scheduled-ingest.service.example \
  /etc/systemd/system/wiki-backend-scheduled-ingest.service
sudo cp docs/wiki-backend-scheduled-ingest.timer.example \
  /etc/systemd/system/wiki-backend-scheduled-ingest.timer
sudo systemd-analyze verify /etc/systemd/system/wiki-backend-scheduled-ingest.service
sudo systemd-analyze verify /etc/systemd/system/wiki-backend-scheduled-ingest.timer
sudo systemctl daemon-reload
sudo systemctl enable --now wiki-backend-scheduled-ingest.timer
systemctl list-timers wiki-backend-scheduled-ingest.timer
```

首次部署先人工运行一次并检查日志；该操作会真实写 MySQL、Wiki 与发布队列：

```bash
sudo systemctl start wiki-backend-scheduled-ingest.service
sudo journalctl -u wiki-backend-scheduled-ingest.service -n 200 --no-pager
```

同步程序会在 journal 和 `/home/dgx/Logs/knowledge_base_mkt/wiki-backend/wiki-backend.log` 记录：
扫描到的 Markdown 总数、每个处理或跳过的相对路径与原因、每次 API 尝试的 job ID，以及最终
`scanned/candidates/succeeded/failed/deferred/skipped` 汇总。服务完成后显示 `inactive (dead)`
是 `Type=oneshot` 的正常结果；应结合 `Result=success` 和上述汇总判断。

定时器使用 `Persistent=true`，主机在 03:00 未运行时，会在恢复后补跑一次。服务运行用户必须能读取
受控源目录，并拥有已有 `wiki-backend.service` 所需的 Wiki、Quartz 发布和 MySQL 权限；不要用 `root`
运行该同步服务。示例中的 `TimeoutStartSec=infinity` 是必要配置：一次同步会逐个等待 Ingest 任务完成，
不能使用 systemd 默认的短启动超时。

### 通过 SSH 隧道访问 FastAPI 文档

FastAPI 文档仍由后端的 `/docs` 路径提供。由于后端只监听 DGX 回环地址，Windows 浏览器不能通过 `<DGX_HOST>:8081/docs` 直接访问。需要在 Windows PowerShell 中建立 SSH 本地端口转发：

```powershell
ssh -N -L 18081:127.0.0.1:8081 <DGX_USER>@<DGX_HOST>
```

保持该 PowerShell 窗口运行，然后在 Windows 浏览器打开：

```text
http://127.0.0.1:18081/docs
```

这里浏览器访问的 `127.0.0.1:18081` 是 Windows 本机端口，SSH 会将请求安全转发到 DGX 的 `127.0.0.1:8081`。关闭 SSH 命令所在窗口后，隧道随即断开。

## DGX Nginx 接入

`wiki-backend` 只接受同机 DGX Nginx 转发：

```nginx
location /api/ {
    proxy_pass http://127.0.0.1:8081;
    proxy_http_version 1.1;

    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    client_max_body_size 100m;
    proxy_connect_timeout 10s;
    proxy_send_timeout 600s;
    proxy_read_timeout 600s;

    proxy_buffering off;
    proxy_cache off;
    add_header X-Accel-Buffering no;
}
```

`proxy_pass` 后不要加 `/`，否则可能剥离 `/api/` 前缀。ECS Nginx 也必须对 `/api/` 禁用缓存和响应缓冲。

生产页面与 API 使用同源 `/api` 后，CORS 不再是主链路依赖。不要为了修复反向代理问题而把 CORS 改成 `*`。

## 健康检查

在 DGX 上先直连后端：

```bash
curl --fail --silent --show-error http://127.0.0.1:8081/api/health
curl --fail --silent --show-error http://127.0.0.1:8081/api/chats > /dev/null
```

再经 DGX Nginx 验证：

```bash
curl --fail --silent --show-error http://127.0.0.1:8080/api/health
curl --fail --silent --show-error http://127.0.0.1:8080/api/chats > /dev/null
```

`/api/health` 只说明 FastAPI 进程可用；`/api/chats` 成功才能基本确认 MySQL 初始化和连接可用。query、ingest、synthesis 还需要分别做业务级验证。

## 测试

单元测试使用 fake service/storage，不要求真实 MySQL 或 LLM：

Windows：

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests
```

DGX：

```bash
.venv/bin/python -m unittest discover -s tests
```

真实 MySQL 集成测试会使用 `.env` 指向的数据库，仅在明确准备好测试数据库后启用：

Windows：

```powershell
$env:WIKI_BACKEND_RUN_MYSQL_INTEGRATION="1"
.venv\Scripts\python.exe -m unittest tests.test_mysql_integration -v
Remove-Item Env:WIKI_BACKEND_RUN_MYSQL_INTEGRATION
```

DGX：

```bash
WIKI_BACKEND_RUN_MYSQL_INTEGRATION=1 \
  .venv/bin/python -m unittest tests.test_mysql_integration -v
```

UUID 主键迁移集成测试会创建并删除独立数据库，只有在显式设置数据库名后才允许执行；该数据库名不得等于应用数据库：

```powershell
$env:WIKI_BACKEND_RUN_MYSQL_MIGRATION_INTEGRATION="1"
$env:WIKI_BACKEND_MYSQL_MIGRATION_TEST_DATABASE="wiki_backend_id_migration_test"
.venv\Scripts\python.exe -m unittest tests.test_uuid_id_migration -v
Remove-Item Env:WIKI_BACKEND_RUN_MYSQL_MIGRATION_INTEGRATION, Env:WIKI_BACKEND_MYSQL_MIGRATION_TEST_DATABASE
```

## 安全边界

- `8081` 只监听 `127.0.0.1`。
- MySQL、Ollama 和其他模型服务不得直接暴露到公网。
- 公网只经 ECS Nginx、单条 FRP 隧道和 DGX Nginx进入。
- `/api/` 绝对不缓存。
- FRP token、数据库密码、模型密钥和真实服务器配置不进入仓库。
- 当前 API 不应被视为已经具备完整公网身份认证；对外开放写接口时，应在 ECS 入口配置 HTTPS、访问控制、速率限制和日志审计。
- 上传大小、类型、耗时和并发限制要同时考虑 Nginx 与应用层，不能只依赖前端校验。

## Quartz 自动发布

ingest 或 synthesis 成功写入 Wiki 后，会加入同一个 Quartz 发布批次。默认静默合并 120 秒，连续变更最长等待 600 秒；`POST /api/publish/jobs` 可提前执行当前批次或重建当前 Wiki。

发布服务将 Wiki 复制为快照，再执行 Quartz build 到 `quartz/.publish/releases/<job-id>`。验证成功后才原子替换 `quartz/public` 链接；失败时旧站点保持可用。每次发布最多保留最近三版成功构建，不需要 reload DGX Nginx。

DGX 上 `wiki-backend` 的运行用户必须同时拥有 `llm-wiki-agent/wiki` 读取权限、`quartz/.publish` 写权限和 `quartz/public` 链接切换权限，并且能执行配置的 Node.js。ECS 静态页与 `contentIndex.json` 仍可能在短缓存到期后才可见。

`/api/publish/` 是会启动构建子进程的写接口。DGX 与 ECS Nginx 都必须为它单独设置 Basic Auth、限流、`proxy_cache off`，且保持 `proxy_pass http://127.0.0.1:8081;` 不带尾部 `/`。若在公网 HTTP 上使用 Basic Auth，凭据会明文传输；应尽快迁移 HTTPS，认证文件和密码不得提交到 Git。
