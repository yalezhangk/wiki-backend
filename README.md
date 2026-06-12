# wiki-backend

`wiki-backend` 是 LLM Wiki 的服务封装层，职责边界如下：

- `llm-wiki-agent`：负责 `ingest`、`lint`、`health`、`query` 等核心知识工作流
- `wiki-backend`：负责把 `query` 封装成 HTTP API，并提供 chat、聊天历史、会话管理
- `quartz`：负责前端渲染和调用 `wiki-backend`

当前实现刻意不重复实现 `llm-wiki-agent` 的 `query` 核心逻辑，而是将 `query` 工作流内聚到 `wiki-backend` 的可导入 service，并复用 `llm-wiki-agent/tools/llm_config.py`。

## API

- `GET /health`
- `POST /api/query`
- `GET /api/sessions`
- `POST /api/sessions`
- `GET /api/sessions/{session_id}/messages`
- `POST /api/sessions/{session_id}/messages`

## 环境要求

- Python 3.10+
- 项目虚拟环境：`.venv`

## 启动

```bash
# 在当前项目的python虚拟环境里,8081端口
python -m app.main
```

```powershell
.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

## 关键环境变量

- `WIKI_AGENT_REPO_PATH`：`llm-wiki-agent` 仓库路径
- `WIKI_BACKEND_DB_PATH`：SQLite 文件路径，默认 `data/wiki_backend.db`
