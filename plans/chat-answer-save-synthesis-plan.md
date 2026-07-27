# Chat 答案保存为 Synthesis 实施计划

## Summary

目标是在现有有状态 Chat 流程上增加“保存优秀答案”能力：用户在 UI 中选中某条已经生成并持久化的 assistant message，调用独立的 `POST /api/synthesis`，后端将该答案保存为：

```text
../llm-wiki-agent/wiki/syntheses/*.md
```

保存时同步维护：

- `wiki/index.md` 的 `## Syntheses` 区块。
- `wiki/log.md` 的操作记录。
- MySQL 中该 assistant message 的保存状态和 synthesis 路径。

现有 `POST /api/query` 不参与该功能，也不做任何修改。

## 现有 Chat 数据流

当前 UI 使用的主要接口：

```text
GET    /api/chats
POST   /api/chats
PATCH  /api/chats/{chat_id}
GET    /api/chats/{chat_id}/messages
POST   /api/chats/{chat_id}/messages
```

一次 `POST /api/chats/{chat_id}/messages` 会：

1. 保存 user message。
2. 读取最近历史消息。
3. 调用 `QueryService.run_chat_turn()`。
4. 保存 assistant message。
5. 将 `sources` 和 `relevant_pages` 一并保存到 MySQL。
6. 返回 `ChatTurnResponse`，其中包含 `user_message` 和 `assistant_message`。

因此保存 synthesis 时不需要重新调用 LLM，也不应该让前端重新提交答案正文。后端只需要根据 `chat_id` 和 `assistant_message_id` 读取已经持久化的答案。

## 范围与边界

本阶段实现：

- 新增 `POST /api/synthesis`。
- 根据 `chat_id + assistant_message_id` 保存指定 assistant answer。
- 自动找到该 assistant answer 前最近的一条 user message，作为原始问题。
- 允许 UI 提供可选 synthesis 标题。
- 未提供标题时使用对应 user message 生成标题。
- 将答案正文、来源、相关页面和 Chat 来源信息写入 Markdown frontmatter。
- 自动生成 `wiki/syntheses/*.md` 文件名。
- 同步更新 `wiki/index.md` 和 `wiki/log.md`。
- 在 MySQL 中记录 `synthesis_path` 和 `synthesized_at`。
- `GET /api/chats/{chat_id}/messages` 返回消息是否已经保存为 synthesis。
- 同一条 assistant message 默认只能保存一次。

本阶段不实现：

- 修改 `POST /api/query`。
- Chat 回答生成后自动保存。
- 重新调用 LLM 改写答案。
- 前端提交或覆盖答案正文。
- 用户指定任意文件路径。
- 修改、删除或重命名已有 synthesis。
- Synthesis 列表和详情 API。
- MySQL 保存 synthesis 正文。
- 后台任务系统。

## 关键设计决策

### 1. 使用独立的 `POST /api/synthesis`

保存动作发生在答案生成之后，是用户对某条 assistant message 的显式操作，不属于发送消息或执行 Query 的流程。

请求：

```json
{
  "chat_id": "4c992874-bc4a-49d4-85dc-e2c784fb1e61",
  "assistant_message_id": 42,
  "title": "MySQL 多轮聊天实现"
}
```

`title` 可省略：

```json
{
  "chat_id": "4c992874-bc4a-49d4-85dc-e2c784fb1e61",
  "assistant_message_id": 42
}
```

响应：

```json
{
  "chat_id": "4c992874-bc4a-49d4-85dc-e2c784fb1e61",
  "assistant_message_id": 42,
  "question_message_id": 41,
  "title": "MySQL 多轮聊天实现",
  "path": "syntheses/mysql-多轮聊天实现.md",
  "created_at": "2026-06-22T06:30:00Z"
}
```

### 2. 不接收答案正文

请求不包含 `content`、`sources` 或 `relevant_pages`。

理由：

- 避免客户端篡改已经生成的答案。
- 确保 synthesis 与 Chat 历史中看到的答案一致。
- 避免前端重复传输较长 Markdown。
- 服务端可以直接使用 MySQL 中持久化的数据。

### 3. 不接收 `save_path`

UI 只需要表达“保存这条答案”，不需要控制服务器文件系统路径。后端根据标题生成文件名，文件固定写入 `wiki/syntheses/`。

这样可以直接消除绝对路径、`..` 和目录逃逸等输入风险。

### 4. Assistant message 校验

保存前必须满足：

- Chat 存在。
- Message 存在且属于指定 Chat。
- Message 的 `role` 必须为 `assistant`。
- Message 尚未保存为 synthesis。
- Message 前存在至少一条 `user` message。

寻找对应问题的规则：

```sql
SELECT ...
FROM chat_messages
WHERE chat_id = %s
  AND role = 'user'
  AND id < %s
ORDER BY id DESC
LIMIT 1
```

这与当前 ChatTurnService 的“先保存 user、再保存 assistant”顺序一致。

### 5. 标题和文件名

标题来源优先级：

1. 请求中的非空 `title`。
2. 对应 user message 的 `content`。

标题处理：

- 折叠换行和多余空白。
- 最长 80 个字符。
- 不调用 LLM。

文件名处理：

- 中文字符保留。
- 英文转小写。
- 空白转换为 `-`。
- 移除 Windows 非法文件名字符和控制字符。
- 文件名主体最长 80 个字符。
- 无法生成有效名称时使用 `synthesis-YYYYMMDD-HHMMSS`。
- 冲突时依次使用 `-2`、`-3` 后缀。
- 最终路径固定为 `syntheses/<slug>.md`。

### 6. 同一答案只保存一次

`chat_messages` 增加：

```text
synthesis_path varchar(500) null
synthesized_at datetime null
```

保存成功后回写这两个字段。重复保存同一 assistant message 返回 `409 Conflict`，响应中说明已经保存的路径。

该状态同时返回给 Chat UI，因此页面刷新后仍能显示“已保存”。

## 目标目录结构

新增：

```text
app/
├── api/
│   └── synthesis.py
├── schemas/
│   └── synthesis.py
└── services/
    └── synthesis_service.py
tests/
├── test_synthesis_api.py
└── test_synthesis_service.py
```

修改：

```text
app/main.py
app/schemas/chat.py
app/services/chat_service.py
app/storage/mysql.py
tests/test_chats_api.py
tests/test_mysql_integration.py
README.md
```

职责方向：

- `api/synthesis.py`：HTTP 输入、依赖注入和异常映射。
- `schemas/synthesis.py`：请求和响应模型。
- `services/synthesis_service.py`：保存流程编排、Markdown 生成、index/log 更新。
- `services/chat_service.py`：读取 Message 和回写 synthesis 状态。
- `storage/mysql.py`：Message 查询和状态持久化。
- `QueryService`、`ChatTurnService` 不承担 synthesis 保存职责。

## Schema 设计

### `SynthesisCreateRequest`

```python
class SynthesisCreateRequest(BaseModel):
    chat_id: str
    assistant_message_id: int
    title: SynthesisTitle | None = None
```

校验规则：

- `chat_id` 非空，最大 36 个字符；如现有 Chat ID 固定为 UUID，可进一步使用 Pydantic UUID 类型。
- `assistant_message_id > 0`。
- `title` 执行 `strip_whitespace=True`、`min_length=1`、`max_length=80`。

### `SynthesisResponse`

```python
class SynthesisResponse(BaseModel):
    chat_id: str
    assistant_message_id: int
    question_message_id: int
    title: str
    path: str
    created_at: datetime
```

### `ChatMessageResponse`

增加：

```python
synthesis_path: str | None = None
synthesized_at: datetime | None = None
```

这样以下两个接口都会返回最新保存状态：

```text
GET  /api/chats/{chat_id}/messages
POST /api/chats/{chat_id}/messages
```

## MySQL 调整

### `chat_messages` 新增字段

```sql
ALTER TABLE chat_messages
    ADD COLUMN synthesis_path VARCHAR(500) NULL
        COMMENT '该助手消息保存成的Synthesis相对路径',
    ADD COLUMN synthesized_at DATETIME NULL
        COMMENT '保存为Synthesis的时间（UTC）';
```

应用的 `initialize()` 负责检查并补齐已有数据库字段，不能只修改 `CREATE TABLE IF NOT EXISTS`。

### Storage 新增方法

```python
def get_message(
    self,
    chat_id: str,
    message_id: int,
) -> ChatMessageResponse | None:
    ...

def get_previous_user_message(
    self,
    chat_id: str,
    before_message_id: int,
) -> ChatMessageResponse | None:
    ...

def mark_message_synthesized(
    self,
    chat_id: str,
    message_id: int,
    synthesis_path: str,
    synthesized_at: datetime,
) -> ChatMessageResponse:
    ...
```

`mark_message_synthesized()` 必须使用条件更新防止并发重复保存：

```sql
UPDATE chat_messages
SET synthesis_path = %s,
    synthesized_at = %s
WHERE chat_id = %s
  AND id = %s
  AND role = 'assistant'
  AND synthesis_path IS NULL
```

## `SynthesisService` 设计

### 公开方法

```python
class SynthesisService:
    def save_chat_answer(
        self,
        *,
        chat_id: str,
        assistant_message_id: int,
        title: str | None,
    ) -> SynthesisResponse:
        ...
```

### 固定流程

1. 校验 Chat 是否存在。
2. 按 `chat_id + assistant_message_id` 读取 Message。
3. 校验角色为 `assistant`。
4. 校验 `synthesis_path` 为空。
5. 查找该 Message 之前最近的 user message。
6. 确定标题和唯一文件名。
7. 生成 synthesis Markdown。
8. 原子写入 synthesis 文件。
9. 更新 `wiki/index.md`。
10. 更新 `wiki/log.md`。
11. 条件更新 MySQL Message 的 synthesis 状态。
12. 返回 `SynthesisResponse`。

### 文件格式

```markdown
---
title: "MySQL 多轮聊天实现"
type: synthesis
tags: []
sources:
  - "PageA"
relevant_pages:
  - "concepts/page-a.md"
source_chat_id: "4c992874-bc4a-49d4-85dc-e2c784fb1e61"
source_question_message_id: 41
source_assistant_message_id: 42
last_updated: 2026-06-22
---

assistant message 中原样保存的 Markdown 答案
```

要求：

- YAML 字符串必须正确转义，禁止直接拼接未处理的标题或来源。
- `sources` 和 `relevant_pages` 使用 MySQL 中 assistant message 的值。
- 正文保持原始 Markdown，不重新格式化、不重新调用 LLM。
- Chat 和 Message ID 写入 frontmatter，保留来源追踪能力。

## Index 和 Log 更新

### `wiki/index.md`

新增条目：

```markdown
- [MySQL 多轮聊天实现](syntheses/mysql-多轮聊天实现.md) — synthesis
```

规则：

- `## Syntheses` 存在时插入该区块顶部。
- 区块不存在时在文件末尾创建。
- 相同路径不得重复插入。
- 不重新格式化其他区块。

### `wiki/log.md`

新增记录：

```markdown
## [2026-06-22] synthesis | MySQL 多轮聊天实现

Saved chat answer 42 from chat 4c992874-bc4a-49d4-85dc-e2c784fb1e61 to syntheses/mysql-多轮聊天实现.md.
```

沿用当前 Wiki 新记录置顶的约定。

## 文件一致性和并发

Synthesis、Index、Log 和 MySQL 无法组成单个数据库事务，因此采用以下策略：

- 进程内互斥锁保护文件名分配和三个 Wiki 文件的组合写操作。
- 所有文件使用“同目录临时文件 + 原子替换”。
- 修改 index 和 log 前保留原内容。
- 任一步骤失败时，删除新 synthesis 文件并恢复 index/log。
- 文件写入成功后再条件更新 MySQL。
- MySQL 更新失败时执行文件补偿回滚。
- 条件更新失败表示并发请求已经保存，当前请求回滚自己创建的文件并返回 `409`。

本阶段保证单进程内一致性，并用 MySQL 条件更新避免同一 Message 被多个进程同时标记成功。完整的跨进程文件互斥留待引入任务队列或文件锁组件时实现。

## 异常设计

新增异常：

```python
class SynthesisServiceError(RuntimeError):
    """保存 Synthesis 失败。"""


class ChatMessageNotFoundError(SynthesisServiceError):
    """指定 Message 不存在或不属于该 Chat。"""


class InvalidSynthesisMessageError(SynthesisServiceError):
    """指定 Message 不是可保存的 assistant answer。"""


class SynthesisAlreadyExistsError(SynthesisServiceError):
    """该 assistant answer 已保存。"""
```

HTTP 映射：

- Pydantic 校验失败 -> `422`
- Chat 不存在 -> `404`
- Message 不存在或不属于 Chat -> `404`
- Message 不是 assistant -> `422`
- 找不到对应 user question -> `409`
- 同一 Message 已经保存 -> `409`
- MySQL 不可用 -> `503`
- Wiki 文件写入失败 -> `500`

捕获异常时必须保留异常链，不允许吞掉文件系统或数据库异常。

## API 路由

新增 `app/api/synthesis.py`：

```python
router = APIRouter(
    prefix="/api/synthesis",
    tags=["synthesis"],
)


@router.post("", response_model=SynthesisResponse)
def create_synthesis(...):
    ...
```

`app/main.py`：

- 初始化或注入 `SynthesisService`。
- 注册 synthesis router。
- `create_app()` 接受可选 fake `synthesis_service`，便于 API 单元测试。
- 不修改 `/api/query` 路由。

## 实施阶段

### 阶段 1：Schema 与 MySQL 字段

执行：

1. 新建 `app/schemas/synthesis.py`。
2. 扩展 `ChatMessageResponse`。
3. 为 `chat_messages` 增加两个字段。
4. 更新 `_message_from_row()` 和所有消息查询字段。
5. 增加 Storage 查询和条件更新方法。

验证：

- 旧数据库启动后自动补齐字段。
- 旧 Message 默认返回 `synthesis_path=null`。
- 可以精确获取属于某个 Chat 的 Message。
- 不能用一个 Chat ID 读取另一个 Chat 的 Message。

### 阶段 2：ChatService 能力

执行：

1. 增加 `get_message()`。
2. 增加 `get_previous_user_message()`。
3. 增加 `mark_message_synthesized()`。

验证：

- Chat 不存在时抛出 `ChatNotFoundError`。
- Message 不存在时抛出明确异常。
- 条件更新可以阻止重复保存。

### 阶段 3：SynthesisService

执行：

1. 实现 Message 角色和状态校验。
2. 实现问题定位和标题生成。
3. 实现文件名生成和冲突后缀。
4. 实现 YAML frontmatter。
5. 实现原子文件写入。
6. 实现 index 和 log 更新。
7. 实现失败补偿回滚。

验证：

- 保存内容完全来自已持久化的 assistant message。
- 不重新调用 LLM。
- 文件固定写入 `wiki/syntheses/`。
- 失败不会留下部分文件或虚假 MySQL 状态。

### 阶段 4：API 集成

执行：

1. 新建 `app/api/synthesis.py`。
2. 在 `create_app()` 中装配服务。
3. 注册 router。
4. 增加异常映射和 OpenAPI 文档。

验证：

- UI 可以通过 `chat_id + assistant_message_id` 保存答案。
- 保存成功返回路径和 Message 关联信息。
- 重复保存返回 `409`。
- `/api/query` 行为和 schema 完全不变。

### 阶段 5：测试和文档

执行：

1. 新增 `tests/test_synthesis_service.py`。
2. 新增 `tests/test_synthesis_api.py`。
3. 扩展 Chat API 测试，验证消息保存状态。
4. 扩展 MySQL 集成测试，验证新增字段和重复保存保护。
5. 更新 README API 列表和 UI 调用示例。

验证命令：

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## 测试计划

### 正常流程

- 保存一条 assistant message 成功。
- 未提供标题时使用对应 user message。
- 提供标题时使用 UI 标题。
- Answer Markdown 原样保存。
- `sources` 和 `relevant_pages` 正确写入 frontmatter。
- Index 增加唯一条目。
- Log 增加保存记录。
- MySQL Message 写入路径和时间。
- 再次读取 Chat Messages 时可以看到保存状态。

### Message 校验

- Chat 不存在返回 `404`。
- Message 不存在返回 `404`。
- Message 属于另一个 Chat 时返回 `404`，不泄露其存在性。
- 保存 user message 返回 `422`。
- Assistant message 前没有 user message 返回 `409`。
- 同一 assistant message 重复保存返回 `409`。

### 标题和文件名

- 中文标题生成合法文件名。
- 英文标题转为小写 kebab-case。
- 标题中的换行和多余空格被折叠。
- Windows 非法字符被移除。
- 同名但来自不同 Message 时使用递增后缀。
- 空白标题由 Pydantic 返回 `422`。

### 一致性和失败

- Synthesis 写入失败时不修改 index、log 和 MySQL。
- Index 更新失败时删除 synthesis 并恢复 index。
- Log 更新失败时恢复 synthesis/index/log。
- MySQL 状态更新失败时回滚 Wiki 文件。
- 并发重复请求只有一个成功。
- 回滚异常被记录，但原始异常链不丢失。

### 回归

- `POST /api/chats/{chat_id}/messages` 原有行为不变。
- `GET /api/chats/{chat_id}/messages` 只增加可选响应字段。
- `/api/query` 请求和响应完全不变。
- 单元测试使用临时 Wiki 目录和 fake storage，不修改真实 `../llm-wiki-agent/wiki`。

## 验收标准

- UI 能对任意已持久化的 assistant answer 调用 `POST /api/synthesis`。
- 后端只根据 Message ID 读取答案，客户端不能替换答案正文或来源。
- 保存结果位于 `wiki/syntheses/*.md`。
- Frontmatter 包含标题、来源、相关页面、Chat ID 和 Message ID。
- `wiki/index.md` 和 `wiki/log.md` 同步更新。
- 页面刷新后仍能从 Chat Message 响应看到 `synthesis_path`。
- 同一答案不能被重复保存。
- 任一步骤失败不会静默留下部分成功状态。
- `/api/query` 不受影响。
- 全部测试使用项目 `.venv` 运行并通过。

## Assumptions

- 一条 assistant message 对应其前面最近的一条 user message。
- 同一条 assistant message 只允许保存成一个 synthesis。
- Synthesis 正文存放在 Wiki 文件中，MySQL 只保存关联路径和时间。
- UI 可以从 ChatTurnResponse 或 ChatMessagesResponse 获取 assistant message ID。
- UI 不需要直接指定服务器文件路径。
- 当前部署主要使用单个 Uvicorn Worker；完整跨进程文件锁后续处理。
