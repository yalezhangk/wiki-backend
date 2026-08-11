from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import create_app
from app.model_profiles import ModelProfileService


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
            ("deepseek-v4-flash", "local-qwen3.6-35b-direct"),
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

        direct = service.resolve_for_turn("local-qwen3.6-35b-direct")
        thinking = service.resolve_for_turn("local-qwen3.6-35b-thinking")

        self.assertEqual(direct.llm_profile.reasoning_effort, "none")
        self.assertEqual(thinking.llm_profile.reasoning_effort, "low")
        self.assertEqual(direct.llm_profile.max_tokens, 1024)
        self.assertEqual(thinking.llm_profile.max_tokens, 2048)


if __name__ == "__main__":
    unittest.main()
