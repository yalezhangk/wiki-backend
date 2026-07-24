from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import PROJECT_ROOT, Settings
from app.main import create_app
from app.schemas.query import CitationResponse, QueryResult
from app.services.ingest_service import IngestService
from app.services.query_service import QueryService, QueryServiceError


class StubQueryService:
    def __init__(self, error: QueryServiceError | None = None) -> None:
        self.error = error

    def run(self, question: str) -> QueryResult:
        if self.error is not None:
            raise self.error
        return QueryResult(
            answer=f"answer:{question}",
            sources=["产品说明"],
            relevant_pages=["sources/产品说明.md"],
            citations=[
                CitationResponse(
                    path="sources/产品说明.md",
                    title="产品说明",
                    kind="source",
                )
            ],
        )


class StartupDependencyTests(unittest.TestCase):
    def test_default_agent_path_is_cross_platform_sibling(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            settings = Settings(_env_file=None)

        self.assertEqual(
            settings.llm_wiki_repo_path,
            (PROJECT_ROOT.parent / "llm-wiki-agent").resolve(),
        )

    def test_ingest_upload_limit_uses_environment_value(self) -> None:
        with patch.dict(
            "os.environ",
            {"WIKI_BACKEND_INGEST_MAX_UPLOAD_BYTES": "12345"},
            clear=True,
        ):
            settings = Settings(_env_file=None)

        self.assertEqual(settings.ingest_max_upload_bytes, 12345)

    def test_ingest_llm_token_limit_uses_environment_value(self) -> None:
        with patch.dict(
            "os.environ",
            {"WIKI_BACKEND_INGEST_LLM_MAX_TOKENS": "12288"},
            clear=True,
        ):
            settings = Settings(_env_file=None)

        self.assertEqual(settings.ingest_llm_max_tokens, 12288)

    def test_query_llm_token_limits_use_environment_values(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "WIKI_BACKEND_LLM_FAST_MAX_TOKENS": "768",
                "WIKI_BACKEND_LLM_MAIN_MAX_TOKENS": "6144",
            },
            clear=True,
        ):
            settings = Settings(_env_file=None)

        self.assertEqual(settings.llm_fast_max_tokens, 768)
        self.assertEqual(settings.llm_main_max_tokens, 6144)

    def test_services_do_not_require_agent_source_code_during_construction(self) -> None:
        missing_root = Path(tempfile.gettempdir()) / "missing-llm-wiki-agent-for-startup-test"

        query_service = QueryService(missing_root)
        ingest_service = IngestService(
            storage=object(),  # type: ignore[arg-type]
            agent_root=missing_root,
            start_worker=False,
        )

        self.assertIsNone(query_service._call_llm_fast)
        self.assertIsNone(query_service._call_llm_main)
        self.assertIsNone(ingest_service._call_llm_main)

    def test_services_load_backend_owned_llm_callers(self) -> None:
        missing_root = Path(tempfile.gettempdir()) / "missing-llm-wiki-agent-for-llm-test"
        query_service = QueryService(missing_root)
        ingest_service = IngestService(
            storage=object(),  # type: ignore[arg-type]
            agent_root=missing_root,
            start_worker=False,
        )

        query_fast, query_main = query_service._load_llm_callers()
        ingest_main = ingest_service._load_llm_caller()

        self.assertEqual(query_fast.__module__, "app.llm_config")
        self.assertEqual(query_main.__module__, "app.llm_config")
        self.assertEqual(ingest_main.__module__, "app.llm_config")

    def test_health_survives_storage_initialization_failure(self) -> None:
        with patch("app.main.storage.initialize", side_effect=RuntimeError("mysql down")):
            with TestClient(create_app()) as client:
                response = client.get("/api/health")
                legacy_response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertEqual(legacy_response.status_code, 404)

    def test_application_description_lists_all_current_api_groups(self) -> None:
        description = create_app(initialize_storage=False).description

        for group in ("health", "query", "chats", "ingest", "synthesis"):
            self.assertIn(f"`{group}`", description)

    def test_query_contract_returns_answer_and_wiki_identifiers(self) -> None:
        client = TestClient(
            create_app(
                query_service=StubQueryService(),  # type: ignore[arg-type]
                initialize_storage=False,
            )
        )

        response = client.post("/api/query", json={"question": "中文路径是否保留？"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "answer": "answer:中文路径是否保留？",
                "sources": ["产品说明"],
                "relevant_pages": ["sources/产品说明.md"],
                "citations": [
                    {
                        "path": "sources/产品说明.md",
                        "title": "产品说明",
                        "kind": "source",
                        "excerpt": None,
                        "relevance": None,
                    }
                ],
            },
        )

    def test_query_contract_maps_service_failure_to_502(self) -> None:
        client = TestClient(
            create_app(
                query_service=StubQueryService(QueryServiceError("query unavailable")),  # type: ignore[arg-type]
                initialize_storage=False,
            )
        )

        response = client.post("/api/query", json={"question": "test"})

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json(), {"detail": "query unavailable"})


if __name__ == "__main__":
    unittest.main()
