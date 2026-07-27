from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

from app.schemas.publish import PublicationResponse

SynthesisTitle = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]


class SynthesisCreateRequest(BaseModel):
    """保存聊天助手回答为 Synthesis 的请求体。"""

    chat_id: str = Field(min_length=1, max_length=36, description="聊天会话 ID。")
    assistant_message_id: int = Field(gt=0, description="要保存的助手消息 ID。")
    title: SynthesisTitle | None = Field(default=None, description="可选 Synthesis 标题。")


class SynthesisResponse(BaseModel):
    """保存 Synthesis 后的响应体。"""

    chat_id: str = Field(description="聊天会话 ID。")
    assistant_message_id: int = Field(description="已保存的助手消息 ID。")
    question_message_id: int = Field(description="该助手回答对应的用户问题消息 ID。")
    title: str = Field(description="Synthesis 标题。")
    path: str = Field(description="Synthesis Markdown 的 Wiki 相对路径。")
    created_at: datetime = Field(description="保存时间，UTC、秒精度。")
    publication: PublicationResponse | None = Field(
        default=None,
        description="站点发布状态。",
    )
