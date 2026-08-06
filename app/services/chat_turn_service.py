from __future__ import annotations

import logging
import time

from app.schemas.chat import ChatTurnResponse
from app.schemas.model_profile import ModelProfileId
from app.model_profiles import ModelProfileService
from app.services.chat_service import ChatService
from app.services.query_service import QueryService

LOGGER = logging.getLogger(__name__)


class ChatTurnService:
    def __init__(
        self,
        chat_service: ChatService,
        query_service: QueryService,
        history_limit: int,
        model_profile_service: ModelProfileService,
    ) -> None:
        self._chat_service = chat_service
        self._query_service = query_service
        self._history_limit = history_limit
        self._model_profile_service = model_profile_service

    def run_turn(
        self,
        chat_id: int,
        content: str,
        model_profile_id: ModelProfileId,
    ) -> ChatTurnResponse:
        model_profile = self._model_profile_service.resolve_for_turn(model_profile_id)
        chat = self._chat_service.get_chat(chat_id)
        existing_message_count = self._chat_service.count_messages(chat_id)
        started_at = time.monotonic()
        LOGGER.info(
            "Chat turn started chat_id=%s model_profile_id=%s model_label=%s "
            "provider=%s model=%s reasoning_mode=%s reasoning_effort=%s",
            chat_id,
            model_profile.id,
            model_profile.label,
            model_profile.llm_profile.provider,
            model_profile.llm_profile.model,
            model_profile.reasoning_mode,
            model_profile.llm_profile.reasoning_effort or "provider_default",
        )

        user_message = self._chat_service.create_message(chat_id=chat_id, role="user", content=content)
        self._chat_service.update_chat_activity(
            chat_id=chat_id,
            updated_at=user_message.created_at,
            last_message_at=user_message.created_at,
        )
        LOGGER.info(
            "Chat user message persisted chat_id=%s user_message_id=%s content_chars=%s",
            chat_id,
            user_message.id,
            len(user_message.content),
        )

        history_messages = self._chat_service.list_recent_messages(
            chat_id=chat_id,
            limit=self._history_limit,
            before_message_id=user_message.id,
        )
        try:
            query_result = self._query_service.run_chat_turn(
                question=user_message.content,
                history_messages=history_messages,
                model_profile=model_profile.llm_profile,
            )
        except Exception:
            LOGGER.exception(
                "Chat turn failed chat_id=%s user_message_id=%s model_profile_id=%s "
                "stage=answer_generation elapsed_ms=%s",
                chat_id,
                user_message.id,
                model_profile.id,
                round((time.monotonic() - started_at) * 1000),
            )
            raise

        assistant_message = self._chat_service.create_message(
            chat_id=chat_id,
            role="assistant",
            content=query_result.answer,
            sources=query_result.sources,
            relevant_pages=query_result.relevant_pages,
            citations=query_result.citations,
            model_profile_id=model_profile.id,
            model_profile_label=model_profile.label,
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

        LOGGER.info(
            "Chat turn completed chat_id=%s user_message_id=%s assistant_message_id=%s "
            "model_profile_id=%s elapsed_ms=%s answer_chars=%s relevant_pages=%s citations=%s",
            chat_id,
            user_message.id,
            assistant_message.id,
            model_profile.id,
            round((time.monotonic() - started_at) * 1000),
            len(assistant_message.content),
            len(query_result.relevant_pages),
            len(query_result.citations),
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
