from __future__ import annotations

from app.schemas.chat import ChatTurnResponse
from app.services.chat_service import ChatService
from app.services.query_service import QueryService


class ChatTurnService:
    def __init__(
        self,
        chat_service: ChatService,
        query_service: QueryService,
        history_limit: int,
    ) -> None:
        self._chat_service = chat_service
        self._query_service = query_service
        self._history_limit = history_limit

    def run_turn(self, chat_id: str, content: str) -> ChatTurnResponse:
        chat = self._chat_service.get_chat(chat_id)
        existing_message_count = self._chat_service.count_messages(chat_id)

        user_message = self._chat_service.create_message(chat_id=chat_id, role="user", content=content)
        self._chat_service.update_chat_activity(
            chat_id=chat_id,
            updated_at=user_message.created_at,
            last_message_at=user_message.created_at,
        )

        history_messages = self._chat_service.list_recent_messages(
            chat_id=chat_id,
            limit=self._history_limit,
            before_message_id=user_message.id,
        )
        query_result = self._query_service.run_chat_turn(
            question=user_message.content,
            history_messages=history_messages,
        )

        assistant_message = self._chat_service.create_message(
            chat_id=chat_id,
            role="assistant",
            content=query_result.answer,
            sources=query_result.sources,
            relevant_pages=query_result.relevant_pages,
        )
        latest_chat = self._chat_service.update_chat_activity(
            chat_id=chat_id,
            updated_at=assistant_message.created_at,
            last_message_at=assistant_message.created_at,
        )

        if existing_message_count == 0 and chat.title == self._chat_service.default_chat_title:
            latest_chat = self._chat_service.rename_chat(
                chat_id=chat_id,
                title=self._generate_title_from_first_question(user_message.content),
            )

        return ChatTurnResponse(
            chat=latest_chat,
            user_message=user_message,
            assistant_message=assistant_message,
        )

    @staticmethod
    def _generate_title_from_first_question(content: str) -> str:
        normalized = " ".join(content.split())
        if len(normalized) <= 24:
            return normalized
        return normalized[:24].rstrip()
