from __future__ import annotations

import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.chats import router as chats_router
from app.api.ingest import router as ingest_router
from app.api.maintenance import router as maintenance_router
from app.api.model_profiles import router as model_profiles_router
from app.api.quality import router as quality_router
from app.api.publish import router as publish_router
from app.api.synthesis import router as synthesis_router
from app.config import settings
from app.logging_config import configure_logging
from app.schemas.query import QueryRequest, QueryResponse
from app.services.chat_service import ChatService
from app.services.chat_turn_service import ChatTurnService
from app.model_profiles import ModelProfileService
from app.services.ingest_service import IngestService
from app.services.health_maintenance_service import HealthMaintenanceService
from app.services.graph_maintenance_service import GraphMaintenanceService
from app.services.lint_maintenance_service import LintMaintenanceService
from app.services.maintenance_service import MaintenanceService
from app.services.quality_report_service import QualityReportService
from app.services.publish_service import PublishService
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
    publish_service: PublishService | None = None,
    synthesis_service: SynthesisService | None = None,
    maintenance_service: MaintenanceService | None = None,
    model_profile_service: ModelProfileService | None = None,
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
        app.state.model_profile_service.refresh_availability()
        if not hasattr(app.state, "quality_report_service"):
            app.state.quality_report_service = QualityReportService(
                wiki_repo_path=Path(settings.llm_wiki_repo_path),
                stale_after_hours=settings.quality_stale_after_hours,
                maintenance_storage=storage,
            )
        if not hasattr(app.state, "wiki_lock"):
            app.state.wiki_lock = threading.RLock()
        if not hasattr(app.state, "publish_service"):
            if app.state.storage_ready and app.state.initialize_storage:
                app.state.publish_service = PublishService(
                    storage=storage,
                    wiki_repo_path=Path(settings.llm_wiki_repo_path),
                    quartz_repo_path=Path(settings.quartz_repo_path),
                    node_executable=settings.publish_node_executable,
                    build_timeout_seconds=settings.publish_build_timeout_seconds,
                    debounce_seconds=settings.publish_debounce_seconds,
                    max_delay_seconds=settings.publish_max_delay_seconds,
                    wiki_lock=app.state.wiki_lock,
                )
            else:
                app.state.publish_service = None
        if not hasattr(app.state, "ingest_service"):
            app.state.ingest_service = IngestService(
                storage=storage,
                agent_root=Path(settings.llm_wiki_repo_path),
                publish_service=app.state.publish_service,
                wiki_lock=app.state.wiki_lock,
            )
        if not hasattr(app.state, "chat_turn_service"):
            app.state.chat_turn_service = ChatTurnService(
                chat_service=app.state.chat_service,
                query_service=app.state.query_service,
                history_limit=settings.chat_history_limit,
                model_profile_service=app.state.model_profile_service,
            )
        if not hasattr(app.state, "synthesis_service"):
            app.state.synthesis_service = SynthesisService(
                chat_service=app.state.chat_service,
                wiki_repo_path=Path(settings.llm_wiki_repo_path),
                publish_service=app.state.publish_service,
                wiki_lock=app.state.wiki_lock,
            )
        if app.state.maintenance_service is None:
            if app.state.storage_ready and app.state.initialize_storage:
                health_service = HealthMaintenanceService(
                    storage=storage,
                    wiki_repo_path=Path(settings.llm_wiki_repo_path),
                    wiki_lock=app.state.wiki_lock,
                )
                graph_service = GraphMaintenanceService(
                    storage=storage,
                    wiki_repo_path=Path(settings.llm_wiki_repo_path),
                    wiki_lock=app.state.wiki_lock,
                )
                lint_service = LintMaintenanceService(
                    storage=storage,
                    wiki_repo_path=Path(settings.llm_wiki_repo_path),
                    wiki_lock=app.state.wiki_lock,
                )
                app.state.maintenance_service = MaintenanceService(
                    storage=storage,
                    handlers={"health": health_service.run, "graph": graph_service.run, "lint": lint_service.run},
                )
            else:
                app.state.maintenance_service = None
        LOGGER.info("wiki-backend started")
        yield

    app = FastAPI(
        title=settings.app_name,
        description=(
            "Wiki Backend API，提供健康检查、模型档案、知识库问答、聊天会话、文档入库、分析保存、Quartz 发布和知识库质量维护能力。"
            "\n\n"
            "接口分为九类："
            "\n"
            "- `health`：用于服务存活检查。"
            "\n"
            "- `model-profiles`：返回服务端允许公开的 Chat 模型档案和内部模型概览。"
            "\n"
            "- `query`：无状态单轮问答，每次请求独立执行，不保存会话历史。"
            "\n"
            "- `chats`：有状态多轮聊天，消息会写入 MySQL，可按会话持续追问。"
            "\n"
            "- `ingest`：上传资料并查询异步入库任务状态；入库成功不代表 Quartz 已发布。"
            "\n"
            "- `synthesis`：根据已持久化的助手消息生成 Wiki Synthesis，不接收回答正文。"
            "\n"
            "- `publish`：异步构建 Quartz 静态站点；入库成功不等于页面已发布。"
            "\n"
            "- `maintenance`：受控的异步知识库维护任务。创建接口仅入队，需通过任务查询接口轮询状态；"
            "health 默认写 `health-report.md`，graph 会写 graph artifact，lint 会写报告与 log；"
            "运行报告不会作为知识页重新参与维护或问答。"
            "\n"
            "- `quality`：最近质量报告的只读快照。该接口不会执行 maintenance 任务、调用 LLM、写入 Wiki 或触发 Quartz 发布。"
        ),
        lifespan=lifespan,
    )
    app.state.initialize_storage = initialize_storage
    app.state.model_profile_service = model_profile_service or ModelProfileService()
    app.state.maintenance_service = maintenance_service
    app.state.quality_report_service = QualityReportService(
        wiki_repo_path=Path(settings.llm_wiki_repo_path),
        stale_after_hours=settings.quality_stale_after_hours,
        maintenance_storage=storage,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:8080",
            "http://localhost:8080"
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
    if publish_service is not None:
        app.state.publish_service = publish_service
    if chat_turn_service is not None:
        app.state.chat_turn_service = chat_turn_service
    if synthesis_service is not None:
        app.state.synthesis_service = synthesis_service

    @app.get(
        "/api/health",
        tags=["health"],
        summary="检查服务状态",
        description="用于确认 FastAPI 服务进程是否正常启动。该请求处理不访问 MySQL 或 LLM，也不依赖其他业务服务。",
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
            "仅检索 `overview.md` 与 sources、entities、concepts、syntheses 目录中的知识页；"
            "不会将 index、log 或运行报告作为回答引用。"
            "\n\n"
            "该接口是无状态的："
            "\n"
            "- 不创建聊天会话"
            "\n"
            "- 不保存消息历史到 MySQL"
            "\n"
            "- 适合临时提问、调试检索质量、验证知识库回答效果"
            "\n\n"
            "响应同时保留 `sources`、`relevant_pages`，并返回可直接映射 Wiki 页面的结构化 `citations`。"
        ),
    )
    def run_query(
        payload: QueryRequest,
        query_service_dependency: QueryService = Depends(get_query_service),
    ) -> QueryResponse:
        """执行一次独立问答并返回答案、来源路径、相关页面和结构化引用。"""
        try:
            result = query_service_dependency.run(payload.question)
        except QueryServiceError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return QueryResponse(
            answer=result.answer,
            sources=result.sources,
            relevant_pages=result.relevant_pages,
            citations=result.citations,
        )

    app.include_router(chats_router)
    app.include_router(model_profiles_router)
    app.include_router(ingest_router)
    app.include_router(maintenance_router)
    app.include_router(quality_router)
    app.include_router(publish_router)
    app.include_router(synthesis_router)
    return app


def get_query_service(request: Request) -> QueryService:
    return request.app.state.query_service


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8081, reload=True)
