from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class QueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)


class QueryResponse(BaseModel):
    answer: str
    sources: list[str] = Field(default_factory=list)


class SessionCreateRequest(BaseModel):
    title: str | None = Field(default=None, max_length=200)


class SessionResponse(BaseModel):
    id: str
    title: str
    created_at: datetime
    updated_at: datetime


class ChatMessageCreateRequest(BaseModel):
    content: str = Field(min_length=1, max_length=4000)


class ChatMessageResponse(BaseModel):
    id: int
    session_id: str
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime


class SessionMessagesResponse(BaseModel):
    session: SessionResponse
    messages: list[ChatMessageResponse]


class ChatTurnResponse(BaseModel):
    session: SessionResponse
    user_message: ChatMessageResponse
    assistant_message: ChatMessageResponse
