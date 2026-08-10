# Chats 后端数据库、服务及接口设计计划

> 归档说明（2026-08-10）：这是 Chats 分层和 MySQL 落地前的历史设计，不是当前接口契约。
> 当前系统已使用数字自增 ID，并已包含 ingest、synthesis、publish、maintenance、quality 和
> model-profiles；现行行为以 `README.md`、`app/` 与 `tests/` 为准。下文中的“当前”均指该计划编写时的基线。

## Summary

目标只围绕聊天页需求设计后端：支持 `chat list`、`new chat`、进入单个 chat 后连续提问，并且同一个 chat 内后续问题会带上最近历史消息参与回答。

技术边界：

- 数据库改为 `MySQL`。
- `query` 继续读取 `../llm-wiki-agent` 的 `wiki/graph/AGENTS.md`。
- 当前只做聊天相关能力，不包含 `ingest`、`lint`、`graph`、`refresh`。
- 首期不做流式输出。
- chat 标题采用“创建时默认标题，首问完成后自动命名”。
- 连续提问采用“带最近历史”的多轮策略，不带全量历史。

## 目标目录结构

当前实现应从平铺的 `app/main.py`、`app/models.py`、`app/storage.py`、`app/query_service.py` 逐步调整为按职责分层的结构。目录结构如下：

```text
wiki-backend/
├─ app/
│  ├─ __init__.py
│  ├─ main.py
│  ├─ config.py
│  ├─ logging_config.py
│  ├─ api/
│  │  ├─ __init__.py
│  │  └─ chats.py
│  ├─ schemas/
│  │  ├─ __init__.py
│  │  ├─ chat.py
│  │  └─ query.py
│  ├─ services/
│  │  ├─ __init__.py
│  │  ├─ chat_service.py
│  │  ├─ chat_turn_service.py
│  │  └─ query_service.py
│  └─ storage/
│     ├─ __init__.py
│     └─ mysql.py
├─ data/
│  └─ ...
├─ plans/
│  └─ chat-backend-query-postgresql-plan.md
├─ tests/
│  ├─ test_chats_api.py
│  ├─ test_chat_turn_service.py
│  └─ test_query_service.py
├─ requirements.txt
└─ README.md
```

### 文件职责

- `app/main.py`：只负责创建 `FastAPI` 应用、注册路由、启动初始化和全局异常处理。
- `app/config.py`：集中管理配置，包括 `WIKI_AGENT_REPO_PATH`、MySQL 连接信息、默认 chat 标题、历史消息条数。
- `app/api/chats.py`：定义聊天相关 HTTP 接口，包括 `GET /api/chats`、`POST /api/chats`、`GET /api/chats/{chat_id}/messages`、`POST /api/chats/{chat_id}/messages`、`PATCH /api/chats/{chat_id}`。
- `app/schemas/chat.py`：定义 chat 和 message 相关 Pydantic schema。
- `app/schemas/query.py`：定义 query 请求、响应以及 query service 输出需要复用的数据结构。
- `app/services/chat_service.py`：管理 chat 元信息和消息读写，不直接调用 LLM。
- `app/services/chat_turn_service.py`：串联 chat 校验、user message 保存、最近历史读取、query 调用、assistant message 保存、chat 更新时间和首问自动命名。
- `app/services/query_service.py`：保留现有 wiki 查询核心逻辑，新增 `run_chat_turn(question, history_messages)`。
- `app/storage/mysql.py`：封装 MySQL 连接、建表、索引、CRUD 和事务边界。
- `tests/test_chats_api.py`：覆盖 HTTP 层接口行为。
- `tests/test_chat_turn_service.py`：覆盖多轮问答编排、首问自动命名、query 失败保留 user message。
- `tests/test_query_service.py`：覆盖 prompt 构造、最近历史限制、来源和 relevant_pages 返回。

### 分层依赖方向

- `api` 只依赖 `services` 和 `schemas`。
- `services` 可依赖 `storage` 和 `schemas`。
- `storage` 不依赖 `api` 或 `services`。
- `query_service` 只读取 `../llm-wiki-agent` 的 wiki 文件和 LLM 配置，不读写 MySQL。
- `main.py` 只做装配，不承载业务逻辑。

## 开发计划

### 阶段 0：开发前确认

目标：确认边界，避免实现过程中扩大范围。

执行项：

1. 确认只实现 Chats 页后端，不实现 `ingest`、`lint`、`graph`、`refresh`。
2. 确认存储后端为 `MySQL`，不再按 PostgreSQL 实现。
3. 确认是否保留旧的 `/api/sessions` 兼容接口；默认不保留，统一切到 `/api/chats`。
4. 确认 `/api/query` 是否继续保留为单轮调试接口；默认保留，避免影响现有调试入口。

验收标准：

- 计划文件、README 和后续代码命名统一使用 `chat`，不再新增 `session` 概念。
- 数据库相关命名统一使用 `mysql`，不出现新的 `postgres` 文件或配置。

### 阶段 1：目录与模块骨架

目标：先建立目标目录结构，再迁移职责。

执行项：

1. 新建 `app/api/`、`app/schemas/`、`app/services/`、`app/storage/`。
2. 新建各目录下的 `__init__.py`。
3. 将现有平铺文件的职责规划到目标模块：
   - `app/models.py` -> `app/schemas/chat.py`、`app/schemas/query.py`
   - `app/query_service.py` -> `app/services/query_service.py`
   - `app/storage.py` -> `app/storage/mysql.py`
   - `app/main.py` 中的 chat 路由 -> `app/api/chats.py`

验收标准：

- 应用入口仍是 `app.main:app`。
- 业务逻辑不继续堆叠到 `app/main.py`。

### 阶段 2：配置与依赖

目标：把 SQLite 配置替换为 MySQL 配置。

执行项：

1. 在 `app/config.py` 中新增 MySQL 配置：
   - `WIKI_BACKEND_MYSQL_HOST`
   - `WIKI_BACKEND_MYSQL_PORT`
   - `WIKI_BACKEND_MYSQL_USER`
   - `WIKI_BACKEND_MYSQL_PASSWORD`
   - `WIKI_BACKEND_MYSQL_DATABASE`
2. 新增聊天配置：
   - `WIKI_BACKEND_DEFAULT_CHAT_TITLE`
   - `WIKI_BACKEND_CHAT_HISTORY_LIMIT`
3. 从配置中移除或废弃 `WIKI_BACKEND_DB_PATH`。
4. 在 `requirements.txt` 中加入 MySQL 驱动，建议优先使用同步驱动 `PyMySQL`，以匹配当前同步 FastAPI 代码风格。
5. 更新 `.env.example`。

验收标准：

- 使用项目内虚拟环境安装依赖。
- 配置加载失败时错误明确。
- README 中不再描述 SQLite 文件路径。

### 阶段 3：Schema 设计

目标：让 HTTP 输入输出结构与计划文件一致。

执行项：

1. 在 `app/schemas/chat.py` 定义：
   - `ChatCreateRequest`
   - `ChatRenameRequest`
   - `ChatResponse`
   - `ChatMessageCreateRequest`
   - `ChatMessageResponse`
   - `ChatMessagesResponse`
   - `ChatTurnResponse`
2. 在 `app/schemas/query.py` 定义：
   - `QueryRequest`
   - `QueryResponse`
   - `QueryResult`
3. 对 `content`、`question`、`title` 使用 Pydantic 做非空和长度校验。
4. message 响应包含 `sources`、`relevant_pages`。
5. chat list 响应包含 `last_message_preview`。

验收标准：

- 空用户消息由 Pydantic 返回 `422`。
- assistant message 可完整返回 `sources` 与 `relevant_pages`。

### 阶段 4：MySQL 存储层

目标：用 MySQL 存储 chat 元信息和消息。

执行项：

1. 在 `app/storage/mysql.py` 中实现连接管理。
2. 实现 `initialize()`，创建 `chats`、`chat_messages` 和必要索引。
3. 实现 chat 操作：
   - `list_chats()`
   - `create_chat()`
   - `get_chat(chat_id)`
   - `rename_chat(chat_id, title)`
   - `update_chat_activity(chat_id, updated_at, last_message_at)`
4. 实现 message 操作：
   - `create_message(chat_id, role, content, sources, relevant_pages)`
   - `list_messages(chat_id)`
   - `list_recent_messages(chat_id, limit, before_message_id)`
   - `count_messages(chat_id)`
5. `sources`、`relevant_pages` 写入时显式保存 JSON 数组，默认 `[]`。
6. 定义存储层异常：
   - `StorageError`
   - `StorageUnavailableError`
   - `ChatNotFoundError`

验收标准：

- 新建空 chat 后能出现在 chat list。
- chat list 按 `updated_at desc` 返回。
- messages 按 `id asc` 返回。
- MySQL 连接失败时能映射为 `503`。

### 阶段 5：ChatService

目标：把 chat 资源管理从 API 层和存储细节中拆出来。

执行项：

1. 在 `app/services/chat_service.py` 实现：
   - `list_chats()`
   - `create_chat()`
   - `get_chat(chat_id)`
   - `rename_chat(chat_id, title)`
   - `list_messages(chat_id)`
   - `list_recent_messages(chat_id, limit, before_message_id)`
   - `create_message(...)`
   - `update_chat_activity(...)`
2. `create_chat()` 默认标题为 `新对话`。
3. `rename_chat()` 去掉换行和多余空格。
4. `ChatService` 不直接调用 LLM。

验收标准：

- chat 不存在时抛出统一的 `ChatNotFoundError`。
- chat title 为空时返回校验错误。

### 阶段 6：QueryService 多轮能力

目标：保留现有 query 核心逻辑，并支持聊天历史上下文。

执行项：

1. 将现有 query 逻辑迁移到 `app/services/query_service.py`。
2. 保留 `run(question)`，作为 `/api/query` 的单轮入口。
3. 新增 `run_chat_turn(question, history_messages)`。
4. prompt 中明确包含：
   - `Conversation history`
   - `Relevant wiki pages`
   - `Current user question`
5. `history_messages` 只使用最近 `6` 条。
6. prompt 明确要求最终答案必须基于 wiki 页面内容。

验收标准：

- 多轮 prompt 不拼接全量历史。
- `QueryResult` 返回 `answer`、`sources`、`relevant_pages`。
- wiki 不可用或 LLM 失败时抛出 `QueryServiceError`，由 API 映射为 `502`。

### 阶段 7：ChatTurnService

目标：实现一次用户提问的完整编排流程。

执行项：

1. 在 `app/services/chat_turn_service.py` 实现 `run_turn(chat_id, content)`。
2. 固定流程：
   - 校验 `chat_id`
   - 保存 user message
   - 读取该 chat 最近 `6` 条历史消息
   - 调用 `QueryService.run_chat_turn()`
   - 保存 assistant message，包含 `sources`、`relevant_pages`
   - 更新 chat 的 `updated_at` 和 `last_message_at`
   - 如果是首轮问答且标题仍为默认标题，自动生成标题
3. 自动标题规则：
   - 使用用户第一问
   - 去掉换行和多余空格
   - 截断为前 `18-24` 个字符
   - 不调用 LLM
4. query 失败时保留 user message，不写 assistant message。

验收标准：

- 首次提问后 chat 自动重命名。
- 第二次提问会带最近历史参与 query。
- query 失败后数据库中只有 user message。

### 阶段 8：API 路由

目标：实现计划文件中的 HTTP 接口。

执行项：

1. 在 `app/api/chats.py` 中实现：
   - `GET /api/chats`
   - `POST /api/chats`
   - `GET /api/chats/{chat_id}/messages`
   - `POST /api/chats/{chat_id}/messages`
   - `PATCH /api/chats/{chat_id}`
2. 在 `app/main.py` 中注册 router。
3. 保留 `GET /api/health`。
4. 如保留 `/api/query`，可放在单独 router 或留在 `main.py`，但不与 chat 业务混写。
5. 错误映射：
   - `ChatNotFoundError` -> `404`
   - Pydantic 校验失败 -> `422`
   - `QueryServiceError` -> `502`
   - `StorageUnavailableError` -> `503`

验收标准：

- API 响应字段与计划文件一致。
- 新发消息后该 chat 排到 chat list 最前。
- 前端刷新后仍能看到 assistant message 的来源信息。

### 阶段 9：测试

目标：用测试锁住关键行为。

执行项：

1. `tests/test_chats_api.py` 覆盖：
   - 创建 chat
   - chat list 排序
   - 获取空 chat messages
   - 不存在 chat 返回 `404`
   - 空消息返回 `422`
2. `tests/test_chat_turn_service.py` 覆盖：
   - 首问保存 user message、assistant message 并自动命名
   - 第二问带最近历史
   - 连续多轮只取最近 `6` 条
   - query 失败保留 user message，不写 assistant message
3. `tests/test_query_service.py` 覆盖：
   - prompt 包含三个明确区块
   - history 超过 `6` 条时只使用最近 `6` 条
   - 返回 `sources` 与 `relevant_pages`
4. 测试中对 LLM 和 MySQL 使用 fake 或 mock，避免单元测试依赖真实外部服务。

验收标准：

- 单元测试可在没有真实 LLM 的环境下运行。
- MySQL 集成测试可作为单独测试组运行。

### 阶段 10：文档与联调

目标：让本地启动和前端联调路径清晰。

执行项：

1. 更新 `README.md`：
   - 环境要求
   - MySQL 初始化说明
   - `.env` 示例
   - 启动命令
   - API 列表
2. 更新 `.env.example`。
3. 使用项目内虚拟环境运行测试和服务。
4. 使用真实 MySQL 做一次手动联调：
   - 创建 chat
   - 发送第一问
   - 刷新读取 messages
   - 发送第二问
   - 检查 chat list 排序和来源保存

验收标准：

- README 中不再出现 SQLite 作为当前方案。
- 开发者可按 README 启动后端并完成 chat 基本流程。

## 数据库设计

### `chats`

用于 chat list 和 chat 元信息。

字段：

- `id char(36) primary key`
- `title text not null`
- `status text not null default 'active'`
- `created_at datetime not null`
- `updated_at datetime not null`
- `last_message_at datetime null`

说明：

- `status` 首期只使用 `active`。
- `updated_at` 用于 chat list 排序。
- `last_message_at` 用于显示最近会话活跃时间。

### `chat_messages`

用于存储单个 chat 内的全部消息。

字段：

- `id bigint primary key auto_increment`
- `chat_id char(36) not null references chats(id)`
- `role varchar(16) not null`
- `content text not null`
- `sources json not null`
- `relevant_pages json not null`
- `created_at datetime not null`

说明：

- `sources`、`relevant_pages` 主要对 assistant message 有意义。
- 前端进入 chat 时，按 `id asc` 读取消息即可。
- 当前不单独建 `query_runs`，避免把聊天页做成复杂审计系统。
- `sources`、`relevant_pages` 写入时显式保存 `[]`，避免依赖不同 MySQL 版本上的 JSON 默认值行为。

### 索引

必须创建：

- `idx_chats_updated_at` on `chats(updated_at desc)`
- `idx_chat_messages_chat_id_id` on `chat_messages(chat_id, id)`
- `idx_chat_messages_chat_id_created_at` on `chat_messages(chat_id, created_at)`

### MySQL 实现说明

- 推荐使用 `InnoDB`。
- 推荐使用 `utf8mb4` 和 `utf8mb4_unicode_ci`。
- `chats.id` 使用应用层生成的 UUID 字符串保存，建议落库为 `char(36)`。
- `chat_messages.chat_id` 与 `chats.id` 类型保持一致。
- `role` 建议用 `varchar(16)` 并在应用层校验取值；如需数据库约束，可在 MySQL 8+ 使用 `check`。

## 服务设计

### `ChatService`

职责：管理 chat 资源本身，不直接调用 LLM。

方法：

- `list_chats()`
- `create_chat()`
- `get_chat(chat_id)`
- `rename_chat(chat_id, title)`
- `list_messages(chat_id)`

行为：

- `create_chat()` 创建默认标题，如 `新对话`。
- `list_chats()` 按 `updated_at desc` 返回。
- `list_messages(chat_id)` 按 `id asc` 返回。

### `QueryService`

职责：基于 wiki 回答问题。

保留现有核心逻辑：

- 从 `index.md` 找相关页面。
- 必要时用 fast model 选页。
- 用 main model 综合回答。
- 返回 `answer`、`sources`、`relevant_pages`。

新增多轮接口：

- `run_chat_turn(question, history_messages)`

prompt 要求：

- 只带最近若干条消息，默认最近 `6` 条。
- prompt 中明确区分 `Conversation history`、`Relevant wiki pages`、`Current user question`。
- 历史消息用于补足指代、省略和上下文延续。
- 最终答案仍必须基于 wiki 页面内容回答。

### `ChatTurnService`

职责：串联 chat、message、query。

固定流程：

1. 校验 `chat_id`。
2. 保存 user message。
3. 读取该 chat 最近 `6` 条历史消息。
4. 调用 `QueryService.run_chat_turn()`。
5. 保存 assistant message。
6. 更新 chat 的 `updated_at` 和 `last_message_at`。
7. 如果这是该 chat 的首轮问答，自动生成标题并更新 chat title。
8. 返回本轮 user/assistant message 和 chat 最新状态。

自动命名规则：

- 仅在 chat 仍为默认标题时触发。
- 使用用户第一问截断生成标题。
- 建议取前 `18-24` 个字符，去掉换行和多余空格。
- 首期不额外调用 LLM 生成标题。

## API 设计

### `GET /api/chats`

用于 chat list 页面。

返回字段：

- `id`
- `title`
- `created_at`
- `updated_at`
- `last_message_at`
- `last_message_preview`

### `POST /api/chats`

用于 `new chat`。

请求体：

- 可为空。

返回：

- 新建 chat 对象。

行为：

- 创建默认标题 `新对话`。
- 不自动插入 assistant welcome message。

### `GET /api/chats/{chat_id}/messages`

用于进入某个 chat 后加载消息列表。

返回：

- `chat`
- `messages`

message 字段：

- `id`
- `role`
- `content`
- `sources`
- `relevant_pages`
- `created_at`

### `POST /api/chats/{chat_id}/messages`

用于在某个 chat 内继续提问。

请求体：

- `content`

返回：

- `chat`
- `user_message`
- `assistant_message`

assistant message 必须包含：

- `sources`
- `relevant_pages`

### `PATCH /api/chats/{chat_id}`

用于重命名 chat。

请求体：

- `title`

## 关键行为约束

### 多轮上下文

- 每次回答只取当前 chat 最近 `6` 条消息参与 prompt。
- 不做历史摘要压缩。
- 不做全量历史拼接。

### Chat 排序

- `GET /api/chats` 固定按 `updated_at desc` 返回。
- 新发消息的 chat 自动排到最前。

### 来源保存

- assistant message 保存时，`sources` 与 `relevant_pages` 一并入库。
- 前端刷新后仍能显示该轮回答的引用来源。

### 错误处理

- `chat_id` 不存在返回 `404`。
- 用户消息为空返回 `422`。
- wiki 不可用或 LLM 失败返回 `502`。
- 数据库不可用返回 `503` 或启动失败。
- 如果 query 失败，保留 user message，不写 assistant message。

## Test Plan

### Chat list / new chat

- 创建新 chat 成功，默认标题为 `新对话`。
- 新建空 chat 后能出现在 chat list。
- chat list 按 `updated_at` 倒序返回。
- 有消息后，该 chat 自动排到最前。

### 单个 chat 消息加载

- 获取某个 chat 的消息列表成功。
- 空 chat 返回空 messages 数组。
- assistant message 的 `sources`、`relevant_pages` 能正确返回。

### 连续提问

- 同一 chat 首次提问时保存 user message、生成 assistant message、自动改标题。
- 同一 chat 第二次提问时带最近历史参与 query。
- 连续多轮后 prompt 仅使用最近 `6` 条消息。

### 异常路径

- 不存在的 `chat_id` 返回 `404`。
- 空消息返回 `422`。
- query 失败时保留 user message，不写 assistant message。
- MySQL 断连时返回明确错误。

## Assumptions

- 当前只设计 Chats 页后端，不包含 `ingest`、synthesis 保存、任务系统。
- `query` 继续基于 `../llm-wiki-agent` 的文件数据执行，不迁移 wiki 内容到数据库。
- MySQL 只存 chat 元数据和消息，不存 wiki 正文。
- chat 首期不做删除、归档、置顶、流式输出。
- chat 标题首期不调用 LLM 生成，只用首问文本截断自动命名。
