# wiki-backend

`wiki-backend` 是 Wiki 系统的 FastAPI 服务层，为 Quartz 前端提供查询、聊天、入库、综合整理、发布、维护和质量检查 API。

项目与同级目录的关系：

- `../quartz`：静态 Wiki 站点和聊天界面；生产环境通过同源 `/api` 调用本服务。
- `../llm-wiki-agent`：共享 Wiki Markdown、附件和图数据；本服务只使用其数据目录，不动态导入或执行 agent 源码。
- MySQL：保存 chat、message、ingest job 等运行时元数据。

本服务不提供 Quartz 静态页面。Ingest 或 synthesis 成功后会触发 Quartz 发布流程，但只有发布任务成功，`quartz/public/` 才是最新内容。

## 部署拓扑

```text
浏览器
  -> DGX Nginx :8080
       -> Quartz 静态文件
       -> /api/* -> wiki-backend 127.0.0.1:8081

公网访问：
浏览器 -> ECS Nginx :8080 -> ECS 127.0.0.1:18080 -> FRP -> DGX Nginx :8080
```

固定边界：

- 生产后端只监听 `127.0.0.1:8081`，不直接暴露到局域网或公网。
- Quartz 使用同源 `/api`，不直连后端端口。
- ECS 只保留到 DGX Nginx `8080` 的业务隧道，不增加后端 `8081` 直通。
- DGX Nginx 的 `proxy_pass` 末尾不加 `/`，并对 `/api/` 禁用缓存和代理缓冲。

## 能力与 API

主要能力：

- 无状态知识库问答，并返回结构化引用。
- MySQL 持久化聊天、消息及回答。
- 文档和 Markdown 入库、任务进度及失败诊断。
- 将回答综合为 Wiki Markdown，并记录 synthesis 状态。
- Quartz 构建与发布状态管理。
- 知识库维护任务、概览和质量报告。
- 服务端白名单控制聊天模型档案，支持 DeepSeek 和 DGX 同机 Ollama。

核心路由：

| 分组 | 路由 |
| --- | --- |
| 健康与模型 | `GET /api/health`、`GET /api/model-profiles`、`GET /api/model-profiles/overview` |
| 无状态查询 | `POST /api/query` |
| 聊天 | `GET/POST /api/chats`、`PATCH /api/chats/{chat_id}`、`GET/POST /api/chats/{chat_id}/messages` |
| 入库 | `GET/POST /api/ingest/jobs`、`GET /api/ingest/jobs/{job_id}` |
| 综合整理 | `POST /api/synthesis` |
| 发布 | `GET /api/publish/status`、`GET/POST /api/publish/jobs`、`GET /api/publish/jobs/{job_id}` |
| 维护与质量 | `/api/maintenance/jobs`、`POST /api/maintenance/workflows/quality`、`GET /api/quality/latest` |

服务启动后，完整请求模型、响应模型和状态码以 FastAPI OpenAPI 为准：

- Swagger UI：`http://127.0.0.1:8081/docs`
- OpenAPI JSON：`http://127.0.0.1:8081/openapi.json`

### 关键行为约定

- `POST /api/query` 是无状态查询，不会隐式创建 chat。
- chat、message 和 ingest job 的外部 ID 为数据库数值主键；跨阶段关联使用独立 workflow UUID。
- API 时间字段使用带 `+08:00` 的北京时间。
- Ingest 会校验文件名、类型、大小和落盘路径；任务详情包含当前阶段和进度。
- 新建 Ingest 任务以文件主名的 NFKC、空白归一化和大小写折叠结果进行全局去重；manual 与 scheduled 同名会返回 `409 Conflict`，失败任务会释放名称。
- 新 manual 文件保存到 `raw/uploads/manual/`；新 scheduled Markdown 保存到 `raw/uploads/scheduled/`。scheduled 必须提交 `source_url`，manual 不得提交该字段。
- Source 页面由后端在写入前确定来源：manual 写 `source_file: raw/uploads/manual/<原文件>`，scheduled 只写 `source_url: "https://..."`；已存在的 Source slug 不会被覆盖。
- 查询引用由服务端解析为结构化 `sources`；正文中的引用标记与 `sources` 顺序对应。
- Synthesis 写入 Wiki Markdown，并更新消息的 synthesis 状态。
- Ingest 和 synthesis 的业务成功不等同于 Quartz 发布成功；发布状态需单独检查。
- 知识页面、研究报告和附件目录有不同的索引、引用与清理规则，详见专项文档。

## 环境要求

- Python 3.10+；Windows 和 DGX 都必须使用项目内 `.venv`。
- MySQL 8.x。
- 可访问的 DeepSeek API，或 DGX 同机 Ollama。
- 同级 `../llm-wiki-agent` 数据仓库。
- 需要处理 PDF/OCR 时，按需安装对应可选依赖。

## 配置

复制示例配置后填写真实值，`.env` 不得提交：

```powershell
Copy-Item .env.example .env
```

关键变量：

| 变量 | 说明 |
| --- | --- |
| `WIKI_AGENT_REPO_PATH` | 共享 Wiki 数据所在的 agent 仓库根目录，默认 `../llm-wiki-agent` |
| `WIKI_BACKEND_MYSQL_*` | MySQL 主机、端口、用户、密码和数据库 |
| `WIKI_BACKEND_LLM_PROVIDER` | 默认模型提供方，供 Query 和维护等内部任务使用 |
| `WIKI_BACKEND_INGEST_PROVIDER` / `WIKI_BACKEND_INGEST_MODEL` | Ingest 专用服务端白名单模型，默认 DeepSeek V4 Pro；不由浏览器请求决定 |
| `WIKI_BACKEND_INGEST_LLM_MAX_TOKENS` | Ingest 单次完整 JSON 响应的最大输出 token，默认 8192 |
| `WIKI_BACKEND_INGEST_REASONING_EFFORT` | 兼容配置项；Ingest 的 DeepSeek 与本地 `qwen3.6:35b` 均固定为 `none`，以确保完整 JSON 输出 |
| `WIKI_BACKEND_DEEPSEEK_API_KEY` | DeepSeek API 密钥 |
| `WIKI_BACKEND_DEEPSEEK_API_BASE` | DeepSeek API 地址 |
| `WIKI_BACKEND_OLLAMA_API_BASE` | Ollama 地址，DGX 同机通常为 `http://127.0.0.1:11434` |
| `WIKI_BACKEND_MODEL_PROFILE_DEFAULT_ID` | 默认聊天模型档案 |
| `WIKI_BACKEND_MODEL_PROFILE_ENABLED_IDS` | 对外公开的模型档案白名单 |
| `WIKI_BACKEND_CHAT_HISTORY_LIMIT` | 构造模型上下文时读取的历史消息数量 |

完整配置、默认值和兼容变量见 [.env.example](.env.example) 与 `app/config.py`。模型名、推理策略、token 和温度由服务端档案固定，前端只能选择已启用的档案 ID。

切换到 DGX 同机 Ollama 前，先验证模型服务：

```bash
curl http://127.0.0.1:11434/api/tags
```

然后在 `.env` 中配置 `WIKI_BACKEND_OLLAMA_API_BASE` 和启用的模型档案，重启 `wiki-backend.service`。不要把 Ollama `11434` 暴露到公网。

## MySQL 初始化

首次部署时由管理员创建数据库和最小权限账号，应用启动时会初始化所需表与索引：

```sql
CREATE DATABASE IF NOT EXISTS wiki_backend
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_0900_ai_ci;

CREATE USER IF NOT EXISTS 'wiki_backend_app'@'localhost'
  IDENTIFIED BY 'replace-with-a-strong-password';

GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX
  ON wiki_backend.* TO 'wiki_backend_app'@'localhost';

FLUSH PRIVILEGES;
```

生产环境不要使用 MySQL `root` 运行应用。历史库主键迁移属于一次性高风险操作，执行前应备份并阅读对应迁移工具的 `--help`，不在日常启动流程中运行。

### 历史 manual 来源迁移

`tools/migrate_ingest_source_origins.py` 只查询和更新 `ingest_jobs.trigger='manual'`。默认 dry-run 会列出每个 manual 任务的旧/新路径、Source 页面和计划回填的名称键，不写文件或数据库；确认输出无误后才执行：

```powershell
.venv\Scripts\python.exe tools\migrate_ingest_source_origins.py
.venv\Scripts\python.exe tools\migrate_ingest_source_origins.py --apply
```

该工具不会移动或修改任何历史 scheduled 文件、任务、Source 页面或 URL。历史 scheduled 任务仍会以只读文件名检查参与新任务的重名判断。

## Windows 开发

除首次创建虚拟环境外，所有 Python 命令都使用项目 `.venv`：

```powershell
# 若 .venv 尚不存在，先用已安装的 Python 3.10+ 创建一次
python -m venv .venv

.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8081 --reload
```

模块入口 `.venv\Scripts\python.exe -m app.main` 也会启动开发服务，但固定开启 reload，不用于生产。

## DGX 安装与启动

以下命令使用项目约定目录；如果 DGX 用户名或路径不同，需要同步替换命令和 systemd 单元中的值。

### 首次安装

```bash
cd /home/dgx/Projects/knowledge_base_mkt/wiki-backend

uv venv --python 3.12
uv pip install --python .venv/bin/python -r requirements.txt

.venv/bin/python -m pip check
.venv/bin/python -c "import pdfplumber, pymupdf; from rapidocr import RapidOCR"
```

先在项目根目录准备 `.env` 和 MySQL，再进行前台验证：

```bash
cd /home/dgx/Projects/knowledge_base_mkt/wiki-backend
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8081
```

另一个终端检查：

```bash
curl --fail --silent --show-error http://127.0.0.1:8081/api/health
curl --fail --silent --show-error http://127.0.0.1:8081/api/chats > /dev/null
```

前台验证通过后按 `Ctrl+C` 停止，改用 systemd 长期运行。生产环境不要使用 `python -m app.main`，因为模块入口会开启 reload。

### 使用 systemd 后台运行

创建 `/etc/systemd/system/wiki-backend.service`：

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

`WorkingDirectory` 必须指向项目根目录，确保 `.env` 和相对路径配置按预期加载。验证并启用：

```bash
sudo systemd-analyze verify /etc/systemd/system/wiki-backend.service
sudo systemctl daemon-reload
sudo systemctl enable --now wiki-backend.service

sudo systemctl status wiki-backend.service --no-pager
sudo systemctl is-active wiki-backend.service
sudo systemctl is-enabled wiki-backend.service
```

### 日常启动、重启与日志

```bash
# 启动
sudo systemctl start wiki-backend.service

# 停止
sudo systemctl stop wiki-backend.service

# 重启
sudo systemctl restart wiki-backend.service

# 状态
sudo systemctl status wiki-backend.service --no-pager

# 最近 200 行 systemd 日志
sudo journalctl -u wiki-backend.service -n 200 --no-pager

# 实时 systemd 日志
sudo journalctl -u wiki-backend.service -f

# 实时应用日志
tail -f /home/dgx/Logs/knowledge_base_mkt/wiki-backend/wiki-backend.log
```

应用日志按固定文件名轮转；当前代码为单文件最大 200 MB、保留 50 个备份。排查请求或 5xx 时，同时查看 `journalctl` 和应用日志。

### 更新代码后的安全重启

依赖或数据库行为有变化时，先停服务、更新依赖并测试：

```bash
cd /home/dgx/Projects/knowledge_base_mkt/wiki-backend
sudo systemctl stop wiki-backend.service

uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m unittest discover -s tests

sudo systemctl start wiki-backend.service
sudo systemctl status wiki-backend.service --no-pager
```

仅有已验证的小改动时可以直接重启：

```bash
sudo systemctl restart wiki-backend.service
```

重启后验证后端直连和 DGX Nginx 两层：

```bash
curl --fail --silent --show-error http://127.0.0.1:8081/api/health
curl --fail --silent --show-error http://127.0.0.1:8081/api/chats > /dev/null
curl --fail --silent --show-error http://127.0.0.1:8080/api/health
curl --fail --silent --show-error http://127.0.0.1:8080/api/chats > /dev/null
```

## DGX Nginx 接入

核心代理规则如下；`proxy_pass` 末尾不能加 `/`：

```nginx
location /api/ {
    proxy_pass http://127.0.0.1:8081;
    proxy_http_version 1.1;

    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    proxy_buffering off;
    proxy_cache off;
    add_header Cache-Control "no-store" always;
}
```

修改后执行：

```bash
sudo nginx -t
sudo systemctl reload nginx
curl --fail --silent --show-error http://127.0.0.1:8080/api/health
```

## 从 Windows 访问 DGX API 文档

后端只监听 loopback。需要调试 `/docs` 时使用 SSH 本地端口转发，不要修改监听地址：

```powershell
ssh -N -L 18081:127.0.0.1:8081 <DGX_USER>@<DGX_HOST>
```

保持窗口运行，然后访问 `http://127.0.0.1:18081/docs`。关闭 SSH 进程后隧道即断开。

## 每日增量 Markdown 入库

定时入库通过独立 systemd timer 调用后端脚本，只扫描配置范围内的新 Markdown，并会写真实 Wiki、MySQL、发布状态且可能调用模型。部署前必须先执行 dry-run/validate，确认目录、时区、锁和 `.env`。

部署模板见 [systemd service 示例](docs/wiki-backend-scheduled-ingest.service.example) 和 [systemd timer 示例](docs/wiki-backend-scheduled-ingest.timer.example)。Ingest 的阶段、进度、上传和 OCR 细节见 [Ingest 说明](docs/ingest.md) 与 [Ingest 流程](docs/ingest-flow.md)。

## 测试

Windows：

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests
```

DGX：

```bash
.venv/bin/python -m unittest discover -s tests
```

真实 MySQL、迁移或模型集成测试默认关闭，只能在已准备好的隔离环境中显式启用。涉及依赖、文件系统、文档解析或模型调用的变化，还需要在 DGX ARM64 上验证。

## 安全与副作用

- 不提交 `.env`、数据库密码、FRP token、模型密钥、日志或真实文档。
- 不把 MySQL、Ollama 或后端 `8081` 暴露到公网。
- 不用 `CORS *` 代替同源反向代理。
- 当前 API 不应被视为已有完整公网身份认证；开放写接口前必须补齐 HTTPS、访问控制、限流、上传限制和审计。
- Ingest 会写知识库并可能调用 LLM；synthesis 会写 Wiki Markdown；chat 会写 MySQL。不要用生产数据做随意验证。
- Quartz 是静态站点，Wiki 内容变更只有在发布成功后才会出现在 `public/`。

## 延伸文档

- [AGENTS.md](AGENTS.md)：项目协作、部署边界和测试要求。
- [docs/ingest.md](docs/ingest.md)：Ingest API、文件处理和错误语义。
- [docs/ingest-flow.md](docs/ingest-flow.md)：Ingest 阶段与数据流。
- [docs/wiki-backend-scheduled-ingest.service.example](docs/wiki-backend-scheduled-ingest.service.example)：DGX 定时入库 service 模板。
- [docs/wiki-backend-scheduled-ingest.timer.example](docs/wiki-backend-scheduled-ingest.timer.example)：DGX 定时入库 timer 模板。
- FastAPI `/docs`：当前 API 的请求与响应契约。
