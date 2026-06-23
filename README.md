# wiki-backend

`wiki-backend` 是 LLM Wiki 的 HTTP 服务封装层。

职责边界：

- `llm-wiki-agent`：负责 `ingest`、`lint`、`health`、`query` 等核心知识工作流。
- `wiki-backend`：负责把 wiki query 封装为 HTTP API，并提供 chat、聊天历史和消息来源保存。
- `quartz`：负责前端渲染和调用 `wiki-backend`。

当前后端只实现聊天页需要的能力，不包含 `ingest`、`lint`、`graph`、`refresh`。

## API

- `GET /health`
- `POST /api/query`
- `GET /api/chats`
- `POST /api/chats`
- `PATCH /api/chats/{chat_id}`
- `GET /api/chats/{chat_id}/messages`
- `POST /api/chats/{chat_id}/messages`
- `POST /api/synthesis`

`POST /api/synthesis` 用于把某条已持久化的助手回答保存为 Wiki Synthesis。前端只提交消息身份，不提交答案正文：

```json
{
  "chat_id": "4c992874-bc4a-49d4-85dc-e2c784fb1e61",
  "assistant_message_id": 42,
  "title": "可选标题"
}
```

保存成功后会写入 `../llm-wiki-agent/wiki/syntheses/*.md`，并更新 `wiki/index.md`、`wiki/log.md` 和该助手消息的 `synthesis_path` / `synthesized_at`。

## 环境要求

- Python 3.10+
- 项目虚拟环境：`.venv`
- MySQL 8+，推荐 `InnoDB`、`utf8mb4`

## MySQL

创建数据库示例：

```sql
CREATE DATABASE wiki_backend
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

CREATE USER 'wiki_backend_app'@'localhost'
  IDENTIFIED BY 'replace-with-a-strong-password';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, REFERENCES
  ON wiki_backend.* TO 'wiki_backend_app'@'localhost';
```

应用启动时会自动创建：

- `chats`
- `chat_messages`
- 必要索引

MySQL 只保存 chat 元数据和消息，不保存 wiki 正文。

## 环境变量

参考 `.env.example`：

```text
WIKI_AGENT_REPO_PATH=..\llm-wiki-agent
WIKI_BACKEND_MYSQL_HOST=127.0.0.1
WIKI_BACKEND_MYSQL_PORT=3306
WIKI_BACKEND_MYSQL_USER=wiki_backend_app
WIKI_BACKEND_MYSQL_PASSWORD=
WIKI_BACKEND_MYSQL_DATABASE=wiki_backend
WIKI_BACKEND_DEFAULT_CHAT_TITLE=新对话
WIKI_BACKEND_CHAT_HISTORY_LIMIT=6
```

## 启动

所有 Python 命令都应使用项目内虚拟环境。

```powershell
.venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8081
```

也可以直接运行：

```powershell
.venv\Scripts\python.exe -m app.main
```

## 测试

单元测试使用 fake service/storage，不依赖真实 MySQL 或 LLM。

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests
```

显式开启真实 MySQL 集成测试时，会使用 `.env` 中配置的数据库，并在结束后清理测试创建的会话：

```powershell
$env:WIKI_BACKEND_RUN_MYSQL_INTEGRATION="1"
.venv\Scripts\python.exe -m unittest tests.test_mysql_integration -v
Remove-Item Env:WIKI_BACKEND_RUN_MYSQL_INTEGRATION
```
