from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.chats import router as chats_router
from app.api.ingest import router as ingest_router
from app.api.synthesis import router as synthesis_router
from app.config import settings
from app.logging_config import configure_logging
from app.schemas.query import QueryRequest, QueryResponse
from app.services.chat_service import ChatService
from app.services.chat_turn_service import ChatTurnService
from app.services.ingest_service import IngestService
from app.services.query_service import QueryService, QueryServiceError
from app.services.synthesis_service import SynthesisService
from app.storage.mysql import storage

configure_logging()
LOGGER = logging.getLogger(__name__)
SERVER_LOGGER = logging.getLogger("uvicorn.error")


def create_app(
    *,
    chat_service: ChatService | None = None,
    chat_turn_service: ChatTurnService | None = None,
    query_service: QueryService | None = None,
    ingest_service: IngestService | None = None,
    synthesis_service: SynthesisService | None = None,
    initialize_storage: bool = True,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.storage_ready = False
        if app.state.initialize_storage:
            try:
                storage.initialize()
                app.state.storage_ready = True
            except Exception:
                LOGGER.exception("wiki-backend started without initialized MySQL storage")
        else:
            app.state.storage_ready = True
        if not hasattr(app.state, "chat_service"):
            app.state.chat_service = ChatService(storage)
        if not hasattr(app.state, "query_service"):
            app.state.query_service = QueryService(Path(settings.llm_wiki_repo_path))
        if not hasattr(app.state, "ingest_service"):
            app.state.ingest_service = IngestService(
                storage=storage,
                agent_root=Path(settings.llm_wiki_repo_path),
            )
        if not hasattr(app.state, "chat_turn_service"):
            app.state.chat_turn_service = ChatTurnService(
                chat_service=app.state.chat_service,
                query_service=app.state.query_service,
                history_limit=settings.chat_history_limit,
            )
        if not hasattr(app.state, "synthesis_service"):
            app.state.synthesis_service = SynthesisService(
                chat_service=app.state.chat_service,
                wiki_repo_path=Path(settings.llm_wiki_repo_path),
            )
        LOGGER.info("wiki-backend started")
        yield

    app = FastAPI(
        title=settings.app_name,
        description=(
            "Wiki Backend API，提供健康检查、单轮知识库问答，以及多轮聊天会话能力。"
            "\n\n"
            "接口分为三类："
            "\n"
            "- `health`：用于服务存活检查。"
            "\n"
            "- `query`：无状态单轮问答，每次请求独立执行，不保存会话历史。"
            "\n"
            "- `chats`：有状态多轮聊天，消息会写入 MySQL，可按会话持续追问。"
        ),
        lifespan=lifespan,
    )
    app.state.initialize_storage = initialize_storage

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:8080",
            "http://localhost:8080",
            "http://192.168.8.8:8080",
        ],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(HTTPException)
    async def log_http_exception(request: Request, exc: HTTPException) -> JSONResponse:
        if exc.status_code >= 500:
            SERVER_LOGGER.error(
                "HTTP %s for %s %s: %s",
                exc.status_code,
                request.method,
                request.url.path,
                exc.detail,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=exc.headers,
        )

    if chat_service is not None:
        app.state.chat_service = chat_service
    if query_service is not None:
        app.state.query_service = query_service
    if ingest_service is not None:
        app.state.ingest_service = ingest_service
    if chat_turn_service is not None:
        app.state.chat_turn_service = chat_turn_service
    if synthesis_service is not None:
        app.state.synthesis_service = synthesis_service

    @app.get(
        "/health",
        tags=["health"],
        summary="检查服务状态",
        description="用于确认 FastAPI 服务进程是否正常启动。该接口不访问 LLM，也不依赖聊天业务。",
    )
    def health() -> dict[str, str]:
        """返回最小化健康检查结果。"""
        return {"status": "ok"}

    @app.post(
        "/api/query",
        response_model=QueryResponse,
        tags=["query"],
        summary="执行单轮知识库问答",
        description=(
            "接收一个用户问题，调用知识库检索与 LLM 生成最终答案。"
            "\n\n"
            "该接口是无状态的："
            "\n"
            "- 不创建聊天会话"
            "\n"
            "- 不保存消息历史到 MySQL"
            "\n"
            "- 适合临时提问、调试检索质量、验证知识库回答效果"
        ),
    )
    def run_query(
        payload: QueryRequest,
        query_service_dependency: QueryService = Depends(get_query_service),
    ) -> QueryResponse:
        """执行一次独立问答并返回答案、来源文件和相关页面。"""
        try:
            result = query_service_dependency.run(payload.question)
        except QueryServiceError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return QueryResponse(
            answer=result.answer,
            sources=result.sources,
            relevant_pages=result.relevant_pages,
        )

    app.include_router(chats_router)
    app.include_router(ingest_router)
    app.include_router(synthesis_router)
    return app


def get_query_service(request: Request) -> QueryService:
    return request.app.state.query_service


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8081, reload=True)
