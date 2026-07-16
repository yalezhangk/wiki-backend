# wiki-backend 项目协作规范

本文件适用于 `wiki-backend/`。上级 `../AGENTS.md` 继续生效；若有冲突，以本文件中更具体的后端约定为准。

## 项目定位

`wiki-backend` 是 FastAPI 服务层，负责：

- 对外提供 health、query、chats、ingest 和 synthesis API。
- 使用 MySQL 保存聊天、消息和 ingest job 元数据。
- 独立实现 HTTP query、ingest 和 synthesis，并在预期业务流程中读写相邻 `llm-wiki-agent` 的 Wiki 数据。
- 由 DGX Nginx 通过同源 `/api/` 提供给 Quartz UI。

它不负责提供 Quartz 静态页面，也不负责自动重建 `quartz/public/`。

## 固定部署边界

```text
浏览器 -> DGX Nginx :8080 -> /api/* -> 127.0.0.1:8081

公网：
ECS Nginx :8080
  -> ECS 127.0.0.1:18080
  -> FRP
  -> DGX Nginx :8080
```

必须保持：

1. 生产后端监听 `127.0.0.1:8081`，不监听公网或局域网接口。
2. ECS 只保留 `18080` 到 DGX Nginx `8080` 的业务隧道。
3. 不增加或恢复 `18081` 到后端的直通。
4. DGX Nginx 使用 `proxy_pass http://127.0.0.1:8081;`，末尾不加 `/`。
5. `/api/` 在 ECS 和 DGX 均不缓存；流式接口关闭代理缓冲。
6. Quartz 生产构建通过同源 `/api` 调用本服务，不直连 `8081`。

## 代码结构

- `app/main.py`：FastAPI 应用、生命周期、中间件和 query/health 路由。
- `app/api/`：chats、ingest、synthesis 路由。
- `app/schemas/`：Pydantic 请求与响应模型。
- `app/services/`：业务编排，不把 HTTP 细节下沉到服务层。
- `app/storage/mysql.py`：MySQL 持久化和初始化。
- `app/config.py`：从 `.env` 读取配置。
- `app/llm_config.py`：后端自有 LiteLLM 调用配置，不依赖 agent 源码。
- `app/prompts/`：后端运行时 Prompt；`agent_instructions.md` 只同步自 `llm-wiki-agent/AGENTS.md`。
- `app/logging_config.py`：应用和 Uvicorn 日志配置。
- `tests/`：API、服务、启动、日志和 MySQL 集成测试。

## Python 规则

- 使用 Python 3.10+。
- 除首次创建虚拟环境外，所有 Python、pip、测试和工具命令都必须使用项目 `.venv`。
- Windows 使用 `.venv\Scripts\python.exe`；DGX 使用 `.venv/bin/python`。
- 遵循 PEP 8，新增和修改的函数保持完整类型标注。
- 边界数据使用 Pydantic 校验。
- 使用 `logging`，禁止用 `print` 代替日志。
- 只在补充上下文、转换语义或清理资源时捕获异常；禁止空 `except` 和吞异常。
- 转换异常语义时保留异常链：`raise ... from exc`。
- 不写死 Windows 路径、真实密码、token、IP 或机器私有目录。

## 修改原则

- 变更必须能直接追溯到用户需求，不顺手重构相邻模块。
- 保持 API 路由层、服务层和存储层的现有职责分离。
- API 请求和响应结构变化必须同步更新 Pydantic schema、路由文档、测试和 README。
- 数据库结构变化必须考虑已有数据、启动初始化、索引和回滚风险。
- 不动态导入或执行 `llm-wiki-agent` 的 Python 源码。
- `app/prompts/agent_instructions.md` 只允许同步 `llm-wiki-agent/AGENTS.md`；禁止混入 `CLAUDE.md`。
- 未经用户明确授权，不修改 `llm-wiki-agent` 源码。ingest/synthesis 运行时对其 `wiki/` 的预期业务写入除外。
- 不为未来 Docker 化提前增加无需求的容器配置；当前默认是 DGX 宿主机 `uv + .venv`。

## 配置约定

配置入口是 `.env` 和 `app/config.py`。当前关键变量：

```env
WIKI_AGENT_REPO_PATH=../llm-wiki-agent
WIKI_BACKEND_MYSQL_HOST=127.0.0.1
WIKI_BACKEND_MYSQL_PORT=3306
WIKI_BACKEND_MYSQL_USER=wiki_backend_app
WIKI_BACKEND_MYSQL_PASSWORD=replace-with-a-strong-password
WIKI_BACKEND_MYSQL_DATABASE=wiki_backend
WIKI_BACKEND_DEFAULT_CHAT_TITLE=新对话
WIKI_BACKEND_CHAT_HISTORY_LIMIT=6
WIKI_BACKEND_LLM_FAST_PROVIDER=deepseek
WIKI_BACKEND_LLM_FAST_MODEL=deepseek-v4-flash
WIKI_BACKEND_LLM_MAIN_PROVIDER=deepseek
WIKI_BACKEND_LLM_MAIN_MODEL=deepseek-v4-pro
WIKI_BACKEND_LLM_API_KEY=
WIKI_BACKEND_LLM_API_BASE=
```

`WIKI_AGENT_REPO_PATH` 只表示共享知识库数据所在的 agent 仓库根目录，不允许再用于 `sys.path` 或动态 Python 导入。

真实 `.env` 不提交。新增配置时必须：

1. 在 `app/config.py` 中提供类型和合理默认值或明确必填语义。
2. 更新 `.env.example`。
3. 更新相关测试和 README。
4. 验证 Windows 和 Linux 路径/编码行为。

## 本地与 DGX 命令

Windows 安装、启动和测试：

```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8081 --reload
.venv\Scripts\python.exe -m unittest discover -s tests
```

DGX 初始化、启动和测试：

```bash
cd /home/dgx/Projects/knowledge_base_mkt/wiki-backend
uv venv --python 3.12
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m unittest discover -s tests
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8081
```

当前显式 Uvicorn 命令和模块内置入口都只监听 `127.0.0.1:8081`，不得改回 `0.0.0.0`。生产环境不要用 `.venv/bin/python -m app.main`，因为模块内置入口会开启 reload；长期运行应使用显式 Uvicorn 参数或等价的 systemd 单元。

从 Windows 浏览器调试 FastAPI `/docs` 时，不得为了恢复 `<DGX_HOST>:8081/docs` 而暴露后端监听地址。应在 Windows PowerShell 建立 SSH 本地端口转发：

```powershell
ssh -N -L 18081:127.0.0.1:8081 <DGX_USER>@<DGX_HOST>
```

保持该 PowerShell 窗口运行，并通过 `http://127.0.0.1:18081/docs` 访问；关闭 SSH 进程后隧道即断开。

## 测试要求

实现或修复功能时先定义可验证结果：

- API 行为变化：补充或更新对应 API 测试。
- 服务编排变化：补充服务层单元测试。
- MySQL 行为变化：优先用 fake 隔离单元测试；必要时再运行显式启用的真实 MySQL 集成测试。
- 启动或依赖变化：运行启动依赖测试和完整测试集。
- 日志变化：验证应用日志与 `uvicorn` / `uvicorn.access` / `uvicorn.error` 记录行为。

默认完整测试：

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests
```

DGX 最终验证：

```bash
.venv/bin/python -m unittest discover -s tests
curl --fail --silent --show-error http://127.0.0.1:8081/api/health
curl --fail --silent --show-error http://127.0.0.1:8081/api/chats > /dev/null
curl --fail --silent --show-error http://127.0.0.1:8080/api/health
```

真实 MySQL 集成测试只能在明确准备好的数据库上运行：

```bash
WIKI_BACKEND_RUN_MYSQL_INTEGRATION=1 \
  .venv/bin/python -m unittest tests.test_mysql_integration -v
```

## 数据和副作用边界

- `POST /api/query` 是无状态问答，不应隐式创建 chat。
- chat API 会写 MySQL，测试时优先使用 fake storage。
- ingest 会创建任务并可能修改 `llm-wiki-agent` 中的知识库内容。
- synthesis 会写 Wiki Markdown 并更新消息 synthesis 状态。
- 对会写真实 Wiki、数据库或调用真实 LLM 的验证，执行前必须明确环境和副作用。
- ingest/synthesis 成功后不要宣称 Quartz 页面已经更新；只有重新构建 `public/` 后静态站点才更新。

## 安全要求

- 不提交 `.env`、数据库密码、FRP token、模型密钥或服务器私有配置。
- 不把 MySQL、Ollama 或后端 `8081` 暴露到公网。
- 不以 `CORS *` 代替正确的同源反向代理。
- 当前 API 不应被视为已有完整公网身份认证。涉及公网写接口时，必须同时审查 ECS HTTPS、访问控制、限流、上传限制和审计日志。
- 上传文件名、类型、大小和落盘路径必须在服务端校验，不能信任浏览器输入。
- 日志不得记录密码、token、完整敏感文档或不必要的请求正文。

## 日志要求

- 统一通过 `app/logging_config.py` 配置日志。
- 需要在终端和文件中看到 Uvicorn 请求或 5xx 时，检查相应 logger 的 handler 和传播关系，不要临时散落 `print`。
- 捕获 5xx 或外部服务错误时保留足够上下文和异常栈，但避免输出敏感内容。
- 修改日志轮转策略前先检查当前代码事实，不以旧文档中的历史方案为准。

## DGX ARM64 验证

Windows 测试通过不是部署完成。涉及依赖、文件系统、子进程、文档解析或模型调用的变化，必须在 DGX 验证：

```bash
uname -m
.venv/bin/python --version
.venv/bin/python -m unittest discover -s tests
```

重点检查：

- 依赖是否提供 ARM64 wheel，或能否可靠源码编译。
- 路径是否使用 `pathlib` / Linux 语义。
- 文件是否为 LF，脚本是否有执行权限。
- `WIKI_AGENT_REPO_PATH`、MySQL 和模型服务是否可达。
- health、query、chat、ingest、synthesis 的端到端行为。

## 完成标准

后端变更只有在以下条件满足后才算完成：

- 变更范围与用户需求一致，未覆盖无关或用户已有修改。
- 对应单元测试通过；高风险变化完成必要的集成验证。
- DGX 上服务以 `127.0.0.1:8081` 启动并通过健康检查。
- DGX Nginx 的同源 `/api/` 路径可用，且未新增第二条 FRP 隧道。
- 数据库、Wiki 和模型相关副作用已明确验证。
- 文档和 `.env.example` 与代码事实同步。
- 未提交密钥、运行日志、缓存、虚拟环境或服务器私有配置。
