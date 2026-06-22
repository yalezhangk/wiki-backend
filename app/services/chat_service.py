from __future__ import annotations

from datetime import datetime

from app.config import settings
from app.schemas.chat import ChatMessageResponse, ChatResponse
from app.storage.mysql import ChatNotFoundError, MySQLStorage


class ChatValidationError(ValueError):
    """Raised when chat input is invalid."""


class ChatService:
    def __init__(self, storage: MySQLStorage) -> None:
        self._storage = storage
        self._default_chat_title = settings.default_chat_title

    @property
    def default_chat_title(self) -> str:
        return self._default_chat_title

    def list_chats(self) -> list[ChatResponse]:
        return self._storage.list_chats()

    def create_chat(self, title: str | None = None) -> ChatResponse:
        normalized_title = self._normalize_optional_text(title) or self._default_chat_title
        return self._storage.create_chat(normalized_title)

    def get_chat(self, chat_id: str) -> ChatResponse:
        chat = self._storage.get_chat(chat_id)
        if chat is None:
            raise ChatNotFoundError(f"chat not found: {chat_id}")
        return chat

    def rename_chat(self, chat_id: str, title: str) -> ChatResponse:
        normalized_title = self._normalize_required_single_line_text(title, "title")
        return self._storage.rename_chat(chat_id, normalized_title)

    def list_messages(self, chat_id: str) -> list[ChatMessageResponse]:
        self.get_chat(chat_id)
        return self._storage.list_messages(chat_id)

    def list_recent_messages(
        self,
        chat_id: str,
        limit: int,
        before_message_id: int | None = None,
    ) -> list[ChatMessageResponse]:
        self.get_chat(chat_id)
        return self._storage.list_recent_messages(chat_id, limit=limit, before_message_id=before_message_id)

    def count_messages(self, chat_id: str) -> int:
        self.get_chat(chat_id)
        return self._storage.count_messages(chat_id)

    def create_message(
        self,
        chat_id: str,
        role: str,
        content: str,
        sources: list[str] | None = None,
        relevant_pages: list[str] | None = None,
    ) -> ChatMessageResponse:
        if role not in {"user", "assistant"}:
            raise ChatValidationError("role must be user or assistant")
        normalized_content = self._normalize_required_multiline_text(content, "content")
        self.get_chat(chat_id)
        return self._storage.create_message(
            chat_id=chat_id,
            role=role,
            content=normalized_content,
            sources=sources,
            relevant_pages=relevant_pages,
        )

    def update_chat_activity(
        self,
        chat_id: str,
        updated_at: datetime,
        last_message_at: datetime | None,
    ) -> ChatResponse:
        return self._storage.update_chat_activity(chat_id, updated_at=updated_at, last_message_at=last_message_at)

    @staticmethod
    def _normalize_optional_text(value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        return normalized or None

    @staticmethod
    def _normalize_required_single_line_text(value: str, field_name: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ChatValidationError(f"{field_name} cannot be empty")
        return normalized

    @staticmethod
    def _normalize_required_multiline_text(value: str, field_name: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ChatValidationError(f"{field_name} cannot be empty")
        return normalized
