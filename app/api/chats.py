from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from app.schemas.chat import (
    ChatCreateRequest,
    ChatMessageCreateRequest,
    ChatMessagesResponse,
    ChatRenameRequest,
    ChatResponse,
    ChatTurnResponse,
)
from app.services.chat_service import ChatService, ChatValidationError
from app.services.chat_turn_service import ChatTurnService
from app.services.query_service import QueryServiceError
from app.storage.mysql import ChatNotFoundError, StorageError, StorageUnavailableError

router = APIRouter(
    prefix="/api/chats",
    tags=["chats"],
)


def get_chat_service(request: Request) -> ChatService:
    return request.app.state.chat_service


def get_chat_turn_service(request: Request) -> ChatTurnService:
    return request.app.state.chat_turn_service


@router.get(
    "",
    response_model=list[ChatResponse],
    summary="获取聊天会话列表",
    description="返回当前系统中已保存的所有聊天会话，通常用于聊天首页展示会话列表。",
)
def list_chats(chat_service: ChatService = Depends(get_chat_service)) -> list[ChatResponse]:
    """列出所有聊天会话及其摘要信息。"""
    try:
        return chat_service.list_chats()
    except StorageUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post(
    "",
    response_model=ChatResponse,
    summary="创建新的聊天会话",
    description=(
        "创建一个新的聊天会话。"
        "\n\n"
        "调用方式："
        "\n"
        "- 可传 `title` 作为会话标题"
        "\n"
        "- 也可不传请求体，服务端会使用默认标题"
    ),
)
def create_chat(
    payload: ChatCreateRequest | None = Body(default=None),
    chat_service: ChatService = Depends(get_chat_service),
) -> ChatResponse:
    """创建聊天会话，并返回新会话信息。"""
    try:
        return chat_service.create_chat(payload.title if payload is not None else None)
    except StorageUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.patch(
    "/{chat_id}",
    response_model=ChatResponse,
    summary="修改聊天会话标题",
    description="根据会话 ID 更新标题，不会修改会话中的历史消息内容。",
)
def rename_chat(
    chat_id: str,
    payload: ChatRenameRequest,
    chat_service: ChatService = Depends(get_chat_service),
) -> ChatResponse:
    """更新指定聊天会话的标题。"""
    try:
        return chat_service.rename_chat(chat_id, payload.title)
    except ChatNotFoundError as exc:
        raise HTTPException(status_code=404, detail="chat not found") from exc
    except ChatValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except StorageUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get(
    "/{chat_id}/messages",
    response_model=ChatMessagesResponse,
    summary="获取某个会话的消息历史",
    description=(
        "返回指定聊天会话的元信息和全部消息记录。"
        "\n\n"
        "适用于："
        "\n"
        "- 打开某个已有聊天窗口"
        "\n"
        "- 恢复上下文"
        "\n"
        "- 查看用户消息与助手回答的完整历史"
    ),
)
def list_chat_messages(
    chat_id: str,
    chat_service: ChatService = Depends(get_chat_service),
) -> ChatMessagesResponse:
    """查询指定会话的完整消息历史。"""
    try:
        chat = chat_service.get_chat(chat_id)
        messages = chat_service.list_messages(chat_id)
    except ChatNotFoundError as exc:
        raise HTTPException(status_code=404, detail="chat not found") from exc
    except StorageUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return ChatMessagesResponse(chat=chat, messages=messages)


@router.post(
    "/{chat_id}/messages",
    response_model=ChatTurnResponse,
    summary="在已有会话中发送一条新消息",
    description=(
        "向指定聊天会话追加一条用户消息，服务端会结合该会话的历史上下文生成新的助手回复，"
        "并把本轮用户消息和助手消息一起保存到 MySQL。"
    ),
)
def send_message(
    chat_id: str,
    payload: ChatMessageCreateRequest,
    chat_turn_service: ChatTurnService = Depends(get_chat_turn_service),
) -> ChatTurnResponse:
    """执行一轮有状态聊天，并持久化本轮消息。"""
    try:
        return chat_turn_service.run_turn(chat_id=chat_id, content=payload.content)
    except ChatNotFoundError as exc:
        raise HTTPException(status_code=404, detail="chat not found") from exc
    except ChatValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except QueryServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except StorageUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
