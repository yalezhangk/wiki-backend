from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from app.schemas.synthesis import SynthesisCreateRequest, SynthesisResponse
from app.services.chat_service import ChatMessageNotFoundError
from app.services.synthesis_service import (
    InvalidSynthesisMessageError,
    SynthesisAlreadyExistsError,
    SynthesisQuestionNotFoundError,
    SynthesisService,
    SynthesisWriteError,
)
from app.storage.mysql import ChatNotFoundError, StorageError, StorageUnavailableError

router = APIRouter(
    prefix="/api/synthesis",
    tags=["synthesis"],
)


def get_synthesis_service(request: Request) -> SynthesisService:
    return request.app.state.synthesis_service


@router.post(
    "",
    response_model=SynthesisResponse,
    summary="保存助手回答为 Synthesis",
    description="根据 chat_id 和 assistant_message_id 读取已持久化的助手回答，并保存为 Wiki Synthesis。",
)
def create_synthesis(
    payload: SynthesisCreateRequest,
    synthesis_service: SynthesisService = Depends(get_synthesis_service),
) -> SynthesisResponse:
    """读取已持久化的助手回答，将其保存为 Wiki Synthesis。"""
    try:
        return synthesis_service.save_chat_answer(
            chat_id=payload.chat_id,
            assistant_message_id=payload.assistant_message_id,
            title=payload.title,
        )
    except (ChatNotFoundError, ChatMessageNotFoundError) as exc:
        raise HTTPException(status_code=404, detail="chat or message not found") from exc
    except InvalidSynthesisMessageError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SynthesisAlreadyExistsError as exc:
        detail: str | dict[str, str] = str(exc)
        if exc.path:
            detail = {"message": str(exc), "path": exc.path}
        raise HTTPException(status_code=409, detail=detail) from exc
    except SynthesisQuestionNotFoundError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except StorageUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except SynthesisWriteError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
