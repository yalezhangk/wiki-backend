from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from time import sleep
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import create_app
from app.model_profiles import AnswerModelProfile, ModelProfileService


class ModelProfileApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = ModelProfileService(availability_checker=lambda profile: profile.location == "cloud")
        self.client = TestClient(
            create_app(
                model_profile_service=self.service,
                chat_turn_service=object(),  # type: ignore[arg-type]
                initialize_storage=False,
            )
        )

    def test_list_returns_only_public_profile_data(self) -> None:
        with patch(
            "app.model_profiles.settings.model_profile_enabled_ids",
            ("deepseek-v4-flash", "local-qwen3.8-27b-direct"),
        ):
            service = ModelProfileService(availability_checker=lambda profile: True)
            client = TestClient(create_app(model_profile_service=service, initialize_storage=False))
            response = client.get("/api/model-profiles")

        self.assertEqual(response.status_code, 200)
        profiles = response.json()
        self.assertNotIn("deepseek-v4-pro", [profile["id"] for profile in profiles])
        self.assertEqual(
            set(profiles[0]),
            {"id", "label", "location", "reasoning_mode", "available", "is_default"},
        )
        self.assertNotIn("api_key", profiles[0])
        self.assertNotIn("api_base", profiles[0])

    def test_overview_returns_current_internal_model_configuration(self) -> None:
        with (
            patch("app.api.model_profiles.settings.llm_provider", "ollama_chat"),
            patch("app.api.model_profiles.settings.llm_fast_model", "fast-runtime-model"),
            patch("app.api.model_profiles.settings.llm_main_model", "main-runtime-model"),
        ):
            response = self.client.get("/api/model-profiles/overview")

        self.assertEqual(response.status_code, 200)
        overview = response.json()
        self.assertEqual(
            overview["fast_model"],
            {"provider": "ollama_chat", "model": "fast-runtime-model"},
        )
        self.assertEqual(
            overview["main_model"],
            {"provider": "ollama_chat", "model": "main-runtime-model"},
        )
        self.assertTrue(overview["chat_models"])

    def test_unknown_profile_is_rejected_before_chat_service(self) -> None:
        response = self.client.post(
            "/api/chats/1/messages",
            json={"content": "hello", "model_profile_id": "untrusted/provider"},
        )

        self.assertEqual(response.status_code, 422)

    def test_litellm_parameters_are_rejected(self) -> None:
        response = self.client.post(
            "/api/chats/1/messages",
            json={
                "content": "hello",
                "model_profile_id": "deepseek-v4-flash",
                "api_base": "http://untrusted.example",
            },
        )

        self.assertEqual(response.status_code, 422)


class ModelProfileServiceTests(unittest.TestCase):
    def test_local_profiles_use_litellm_reasoning_effort_mapping(self) -> None:
        service = ModelProfileService(availability_checker=lambda profile: True)

        direct = service.resolve_for_turn("local-qwen3.8-27b-direct")
        thinking = service.resolve_for_turn("local-qwen3.8-27b-thinking")

        self.assertEqual(direct.llm_profile.reasoning_effort, "none")
        self.assertEqual(thinking.llm_profile.reasoning_effort, "low")
        self.assertEqual(direct.llm_profile.max_tokens, 1536)
        self.assertEqual(thinking.llm_profile.max_tokens, 4096)

    def test_list_refreshes_stale_shared_local_model_availability(self) -> None:
        current_time = [0.0]
        available = [False]
        checked_profile_ids: list[str] = []

        def check_availability(profile: AnswerModelProfile) -> bool:
            checked_profile_ids.append(profile.id)
            return available[0]

        with patch(
            "app.model_profiles.settings.model_profile_enabled_ids",
            ("local-qwen3.8-27b-direct", "local-qwen3.8-27b-thinking"),
        ):
            service = ModelProfileService(
                availability_checker=check_availability,
                now=lambda: current_time[0],
            )

        service.refresh_availability()
        self.assertEqual(len(checked_profile_ids), 1)
        self.assertTrue(all(not profile.available for profile in service.list_profiles()))

        available[0] = True
        current_time[0] = 60.0
        self.assertTrue(all(profile.available for profile in service.list_profiles()))
        self.assertEqual(len(checked_profile_ids), 2)

        available[0] = False
        current_time[0] = 120.0
        self.assertTrue(all(not profile.available for profile in service.list_profiles()))
        self.assertEqual(len(checked_profile_ids), 3)

    def test_concurrent_stale_lists_share_one_health_check(self) -> None:
        current_time = [0.0]
        check_count = 0
        check_count_lock = Lock()

        def check_availability(profile: AnswerModelProfile) -> bool:
            nonlocal check_count
            with check_count_lock:
                check_count += 1
            sleep(0.05)
            return True

        with patch(
            "app.model_profiles.settings.model_profile_enabled_ids",
            ("local-qwen3.8-27b-direct", "local-qwen3.8-27b-thinking"),
        ):
            service = ModelProfileService(
                availability_checker=check_availability,
                now=lambda: current_time[0],
            )

        service.refresh_availability()
        current_time[0] = 60.0
        barrier = Barrier(8)

        def list_profiles() -> None:
            barrier.wait()
            service.list_profiles()

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(list_profiles) for _ in range(8)]
            for future in futures:
                future.result()

        self.assertEqual(check_count, 2)


if __name__ == "__main__":
    unittest.main()
