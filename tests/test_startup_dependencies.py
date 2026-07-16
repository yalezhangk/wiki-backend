from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import PROJECT_ROOT, Settings
from app.main import create_app
from app.services.ingest_service import IngestService
from app.services.query_service import QueryService


class StartupDependencyTests(unittest.TestCase):
    def test_default_agent_path_is_cross_platform_sibling(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            settings = Settings(_env_file=None)

        self.assertEqual(
            settings.llm_wiki_repo_path,
            (PROJECT_ROOT.parent / "llm-wiki-agent").resolve(),
        )

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


if __name__ == "__main__":
    unittest.main()
