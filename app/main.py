from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from app.logging_config import configure_logging
from app.models import (
    ChatMessageCreateRequest,
    ChatTurnResponse,
    QueryRequest,
    QueryResponse,
    SessionCreateRequest,
    SessionMessagesResponse,
    SessionResponse,
)
from app.query_service import QueryServiceError, query_service
from app.storage import storage

configure_logging()
LOGGER = logging.getLogger(__name__)

app = FastAPI(title="wiki-backend")

app.add_middleware(
    CORSMiddleware,
    # allow_origins=["*"],
    allow_origins=[
        "http://127.0.0.1:8080",
        "http://localhost:8080"
        ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)



@app.on_event("startup")
def on_startup() -> None:
    storage.initialize()
    LOGGER.info("wiki-backend started")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/query", response_model=QueryResponse)
def run_query(request: QueryRequest) -> QueryResponse:
    try:
        result = query_service.run(request.question)
    except QueryServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return QueryResponse(answer=result.answer, sources=result.sources)


@app.get("/api/sessions", response_model=list[SessionResponse])
def list_sessions() -> list[SessionResponse]:
    return storage.list_sessions()


@app.post("/api/sessions", response_model=SessionResponse)
def create_session(request: SessionCreateRequest) -> SessionResponse:
    return storage.create_session(request.title)


@app.get("/api/sessions/{session_id}/messages", response_model=SessionMessagesResponse)
def list_session_messages(session_id: str) -> SessionMessagesResponse:
    session = storage.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return SessionMessagesResponse(session=session, messages=storage.list_messages(session_id))


@app.post("/api/sessions/{session_id}/messages", response_model=ChatTurnResponse)
def send_message(session_id: str, request: ChatMessageCreateRequest) -> ChatTurnResponse:
    session = storage.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")

    user_message = storage.add_message(session_id=session_id, role="user", content=request.content)

    try:
        result = query_service.run(request.content)
    except QueryServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    assistant_message = storage.add_message(
        session_id=session_id,
        role="assistant",
        content=result.answer,
    )
    latest_session = storage.get_session(session_id)
    if latest_session is None:
        raise HTTPException(status_code=500, detail="session disappeared")

    return ChatTurnResponse(
        session=latest_session,
        user_message=user_message,
        assistant_message=assistant_message,
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=8081, reload=True)
