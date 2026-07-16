# wiki-backend

`wiki-backend` 是 LLM Wiki 的 HTTP API 服务。它把 `llm-wiki-agent` 的 query、ingest 和 Wiki 写入能力封装为 FastAPI 接口，同时使用 MySQL 保存聊天、消息和 ingest 任务状态。

## 职责边界

- `llm-wiki-agent`：知识库 ingest、query、lint、health、graph 等核心工作流及 Wiki 文件。
- `wiki-backend`：HTTP API、聊天编排、任务状态、数据库持久化和 synthesis 写入协调。
- `quartz`：读取 Wiki Markdown，构建静态 UI，并通过同源 `/api` 调用本服务。

`wiki-backend` 不负责直接提供 Quartz 静态页面，也不负责在 ingest 完成后自动重建 `quartz/public/`。当前流程中，Wiki 文件变化后仍需单独执行 Quartz 构建。

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

服务启动后可查看 FastAPI 文档：

```text
http://127.0.0.1:8081/docs
```

生产浏览器应通过同源入口访问，例如 `<SITE_ORIGIN>/api/health`，而不是直接请求 `8081`。

### synthesis 请求示例

```json
{
  "chat_id": "4c992874-bc4a-49d4-85dc-e2c784fb1e61",
  "assistant_message_id": 42,
  "title": "可选标题"
}
```

前端只提交消息身份，不提交答案正文。成功后，服务会在配置的 `llm-wiki-agent` 仓库内写入 `wiki/syntheses/*.md`，更新相关 Wiki 索引和日志，并记录消息的 synthesis 状态。

## 环境要求

- Python 3.10+
- 项目内虚拟环境 `.venv`
- MySQL 8+，推荐 InnoDB 和 `utf8mb4`
- `llm-wiki-agent` 与本项目在 DGX 上可通过 `WIKI_AGENT_REPO_PATH` 互相定位
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
```

真实 `.env` 不提交 Git。DGX 上使用 Linux 路径，不要写入 Windows 反斜杠路径。

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
- 必要索引

MySQL 保存业务元数据，不保存 Wiki Markdown 正文。

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

.venv/bin/python -m uvicorn app.main:app \
  --host 127.0.0.1 \
  --port 8081
```

当前显式 Uvicorn 命令和模块内置入口都只监听 `127.0.0.1:8081`，不会监听 `0.0.0.0`，因此不能再通过 DGX 的局域网地址直接访问 `8081`。生产环境仍不要直接执行 `.venv/bin/python -m app.main`，因为模块内置入口会启用 reload；长期运行应使用上面的显式 Uvicorn 命令，或使用等价的 systemd 服务配置。

依赖更新后应先在 DGX ARM64 上重新安装和验证，再重启长期运行进程。

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

## 安全边界

- `8081` 只监听 `127.0.0.1`。
- MySQL、Ollama 和其他模型服务不得直接暴露到公网。
- 公网只经 ECS Nginx、单条 FRP 隧道和 DGX Nginx进入。
- `/api/` 绝对不缓存。
- FRP token、数据库密码、模型密钥和真实服务器配置不进入仓库。
- 当前 API 不应被视为已经具备完整公网身份认证；对外开放写接口时，应在 ECS 入口配置 HTTPS、访问控制、速率限制和日志审计。
- 上传大小、类型、耗时和并发限制要同时考虑 Nginx 与应用层，不能只依赖前端校验。

## 发布后的 Quartz 更新

ingest 或 synthesis 可能修改 `llm-wiki-agent/wiki`。这些变化不会自动更新 Quartz 已生成的 `public/`。当前发布步骤是：

```bash
cd /home/dgx/Projects/knowledge_base_mkt/quartz
CHAT_PROXY_URL=/api npx quartz build \
  -d /home/dgx/Projects/knowledge_base_mkt/llm-wiki-agent/wiki
```

构建完成后验证 DGX 页面，并根据需要等待或清理 ECS 短缓存。
