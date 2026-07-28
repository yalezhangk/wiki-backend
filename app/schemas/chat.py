from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

from app.schemas.query import CitationResponse

NonEmptyContent = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
ChatTitle = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class ChatCreateRequest(BaseModel):
    """创建聊天会话请求体。"""

    title: str | None = Field(
        default=None,
        max_length=200,
        description="可选的会话标题。不传时由服务端生成默认标题。",
        examples=["产品需求讨论"],
    )


class ChatRenameRequest(BaseModel):
    """重命名聊天会话请求体。"""

    title: ChatTitle = Field(
        description="新的会话标题，不能为空，最大 200 个字符。",
        examples=["数据库方案评审"],
    )


class ChatResponse(BaseModel):
    """聊天会话摘要信息。"""

    id: int = Field(gt=0, description="聊天会话数字 ID。")
    title: str = Field(description="聊天会话标题。")
    status: str = Field(description="会话状态。当前实现通常为 active。")
    created_at: datetime = Field(description="会话创建时间，UTC、秒精度。")
    updated_at: datetime = Field(description="会话最近更新时间，UTC、秒精度。")
    last_message_at: datetime | None = Field(default=None, description="最近一条消息的创建时间，UTC、秒精度。")
    last_message_preview: str | None = Field(
        default=None,
        description="最近一条消息的摘要预览，便于会话列表展示。",
    )


class ChatMessageCreateRequest(BaseModel):
    """发送聊天消息请求体。"""

    content: NonEmptyContent = Field(
        description="用户发送的消息内容。该内容会结合历史消息参与本轮回答生成。",
        examples=["请继续解释上一条回答中的数据库设计。"],
    )


class ChatMessageResponse(BaseModel):
    """单条聊天消息。"""

    id: int = Field(description="消息 ID。")
    chat_id: int = Field(gt=0, description="所属聊天会话数字 ID。")
    role: Literal["user", "assistant"] = Field(description="消息角色，`user` 表示用户，`assistant` 表示助手。")
    content: str = Field(description="消息正文内容。")
    sources: list[str] = Field(
        default_factory=list,
        description="助手回答中 `[[...]]` 提取出的 Wiki 标识列表；用户消息通常为空列表。",
    )
    relevant_pages: list[str] = Field(
        default_factory=list,
        description="助手回答检索时使用的 Wiki 根目录相对路径列表；用户消息通常为空列表。",
    )
    citations: list[CitationResponse] = Field(
        default_factory=list,
        description="助手回答的结构化 Wiki 引用；用户消息和旧历史消息通常为空列表。",
    )
    created_at: datetime = Field(description="消息创建时间，UTC、秒精度。")
    synthesis_path: str | None = Field(
        default=None,
        description="该助手消息保存成 Synthesis 后的 Wiki 相对路径。",
    )
    synthesized_at: datetime | None = Field(
        default=None,
        description="该助手消息保存为 Synthesis 的时间，UTC、秒精度。",
    )


class ChatMessagesResponse(BaseModel):
    """聊天历史查询响应体。"""

    chat: ChatResponse = Field(description="当前会话的基本信息。")
    messages: list[ChatMessageResponse] = Field(description="该会话下的全部消息列表。")


class ChatTurnResponse(BaseModel):
    """单轮聊天响应体。"""

    chat: ChatResponse = Field(description="本轮聊天完成后的会话信息。")
    user_message: ChatMessageResponse = Field(description="本轮刚写入的用户消息。")
    assistant_message: ChatMessageResponse = Field(description="本轮生成并写入的助手回复。")
