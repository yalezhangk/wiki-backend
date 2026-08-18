# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目定位

`wiki-backend` 是行业知识库系统的 FastAPI 服务层，为 Quartz 静态前端提供同源 `/api`。它读取并写入同级 `llm-wiki-agent` 仓库的 Wiki 数据（`wiki/`、`raw/uploads/`、`graph/`），用 MySQL 保存聊天、消息、ingest/发布/维护任务等运行时元数据（不存 Wiki 正文）。

完整的部署拓扑、能力/API 清单、配置项、安全与副作用边界见 [README.md](README.md) 与 [AGENTS.md](AGENTS.md)；上级 [../AGENTS.md](../AGENTS.md) 覆盖三机（Windows 开发 / DGX ARM64 运行 / ECS 公网入口）的整体规范。三者冲突时以本仓库更具体的约定为准。

## 常用命令

所有 Python 命令必须使用项目内 `.venv`（禁止系统 Python）。Windows 用 `.venv\Scripts\python.exe`，DGX 用 `.venv/bin/python`。

```powershell
# 安装依赖（首次或依赖变化时）
.venv\Scripts\python.exe -m pip install -r requirements.txt

# 启动开发服务（显式 Uvicorn 入口；reload 仅用于开发）
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8081 --reload

# 完整测试
.venv\Scripts\python.exe -m unittest discover -s tests

# 单个测试文件 / 单个测试用例
.venv\Scripts\python.exe -m unittest tests.test_query_service -v
.venv\Scripts\python.exe -m unittest tests.test_query_service.QueryServiceTest.test_run -v
```

注意事项：

- `python -m app.main` 会固定开启 reload，仅用于开发，生产用显式 Uvicorn 参数或 systemd 单元。
- 真实 MySQL 集成测试默认关闭，仅在准备好隔离库后显式启用：`WIKI_BACKEND_RUN_MYSQL_INTEGRATION=1 .venv/bin/python -m unittest tests.test_mysql_integration -v`。
- 历史迁移工具默认 dry-run，确认后加 `--apply`：`.venv\Scripts\python.exe tools\migrate_ingest_source_origins.py`。
- 无代码级 linter/formatter 配置；验证以 unittest 为准。`app/services/lint_maintenance_service.py` 是「知识库 Wiki 页面 lint」业务功能，不是代码 lint。

## 架构总览

分层：`app/api/`（路由）→ `app/services/`（业务编排，不含 HTTP 细节）→ `app/storage/mysql.py`（MySQL 持久化）。Pydantic 模型在 `app/schemas/`。配置在 `app/config.py`（pydantic-settings，`validation_alias` 映射 `WIKI_BACKEND_*` 环境变量，全局单例 `settings`）。

### 依赖注入与 `app.state`

- `app/main.py` 的 `create_app()` 是应用工厂：在 `lifespan` 里按依赖顺序构造所有 service 并挂到 `app.state` 上（如 `chat_service`、`query_service`、`ingest_service`、`publish_service`、`synthesis_service`、`maintenance_service`、`quality_report_service`、`model_profile_service`）。
- 路由通过 `Depends(get_*)`（定义在 `app/main.py` 底部和 `app/main_dependencies.py`）从 `request.app.state` 取 service；`get_publish_service` / `get_maintenance_service` / `get_quality_report_service` 在 service 为 `None` 时返回 503。
- service 通过**构造函数**接收 `storage` 和其它 service，不做全局单例（例外：`app.storage.mysql.storage` 是全局 `MySQLStorage`，`app.config.settings` 是全局配置）。`PublishService` 用 `Protocol`（`PublishStorage`）声明存储接口，测试可注入 fake，无需真实 MySQL。
- `create_app(..., initialize_storage=False, xxx_service=<fake>)` 供测试构建不连库的 app。`/api/query` 与 `/api/health` 直接在 `main.py` 内联定义，其余端点从 `app/api/` 挂载 router。

### 后台任务模型（进程内，非可恢复队列）

ingest、publish、maintenance 三类异步任务都由 **daemon worker 线程**消费，任务状态持久化在 MySQL：

- **ingest**：`IngestService` 内部 `Queue[int]` + 单 worker，job ID 入队后 `POST` 立即返回 202。进程重启后内存队列丢失，靠 `storage.recover_*` 兜底。
- **publish**：`PublishService` worker 轮询 `claim_due_publish_job`，通过 debounce/max_delay 把连续 Wiki 变更合并成一次构建；构建走 `node quartz/bootstrap-cli.mjs` 子进程，用 `quartz/public` 符号链接原子切换，保留 3 个 release。
- **maintenance**：`MaintenanceService` 按 `handlers` 字典（health/graph/lint）分发到对应 `*_maintenance_service`。

Ingest/synthesis **业务成功 ≠ Quartz 发布成功**：成功后只是把变更加入 publish 队列，只有 `publication.status=published` 或发布任务成功才算 `quartz/public` 已更新。

### 两条 LLM 路径（勿混淆）

1. **内部任务**（检索、入库、巡检）：`app/llm_config.py` 通过 LiteLLM `completion()` 调用，用 `WIKI_BACKEND_LLM_PROVIDER` + `FAST/MAIN_MODEL`（`call_llm_fast` / `call_llm_main`）。
2. **聊天回答模型档案**：`app/model_profiles.py` 的 `ModelProfileService` 维护服务端白名单档案（deepseek-v4-* 云端 + local-qwen3.6-* 同机 Ollama），模型名/token/温度/推理策略由服务端固定，前端只能选已启用档案 ID（`WIKI_BACKEND_MODEL_PROFILE_ENABLED_IDS`）。聊天走 `call_llm_profile`，不走内部 fast/main。

### Wiki 数据边界

- 后端只读写 `WIKI_AGENT_REPO_PATH` 指向仓库的数据目录（`wiki/`、`raw/uploads/`、`graph/graph.json`），**不动态 import 或执行 `llm-wiki-agent` 的 Python 源码**。
- 「知识页」由 `app/services/wiki_page_policy.py` 定义：`overview.md` + `sources/`、`entities/`、`concepts/`、`syntheses/` 目录下的 `.md`。问答、图谱、巡检只认知识页，不把 `index.md`、`log.md`、运行报告当证据引用。
- 时间统一走 `app/time_utils.beijing_now()`（北京时间、无时区标记、秒精度），用于 MySQL DATETIME 写入，勿用 `datetime.utcnow()` 等。

### Prompt 与日志

- `app/prompts/` 经 `load_prompt` / `render_prompt`（`string.Template`）加载；`agent_instructions.md` 只从 `llm-wiki-agent/AGENTS.md` 同步，禁止混入 `CLAUDE.md`。
- 日志统一由 `app/logging_config.py` 配置，使用 `logging`（禁止 `print`）；不要为看 5xx 临时散落 `print`。
