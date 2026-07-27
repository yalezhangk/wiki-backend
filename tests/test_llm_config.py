from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.config import settings
from app.llm_config import LLMConfigError, LLMResponseTruncatedError, _resolve_model, call_llm_fast


class LLMConfigTests(unittest.TestCase):
    def test_resolve_model_adds_provider_prefix(self) -> None:
        self.assertEqual(_resolve_model("deepseek", "deepseek-chat"), "deepseek/deepseek-chat")
        self.assertEqual(_resolve_model("ollama", "ollama/llama3.1"), "ollama/llama3.1")

    @patch("app.llm_config.completion")
    def test_fast_call_uses_backend_settings(self, completion_mock: Mock) -> None:
        completion_mock.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="answer"))]
        )

        with (
            patch.object(settings, "llm_fast_provider", "deepseek"),
            patch.object(settings, "llm_fast_model", "deepseek-chat"),
            patch.object(settings, "llm_api_key", "test-key"),
            patch.object(settings, "llm_api_base", "http://llm.local"),
        ):
            result = call_llm_fast("prompt", max_tokens=512)

        self.assertEqual(result, "answer")
        completion_mock.assert_called_once()
        kwargs = completion_mock.call_args.kwargs
        self.assertEqual(kwargs["model"], "deepseek/deepseek-chat")
        self.assertEqual(kwargs["max_tokens"], 512)
        self.assertEqual(kwargs["api_key"], "test-key")
        self.assertEqual(kwargs["api_base"], "http://llm.local")

    @patch("app.llm_config.completion")
    def test_empty_llm_response_is_rejected(self, completion_mock: Mock) -> None:
        completion_mock.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=None))]
        )

        with self.assertRaises(LLMConfigError):
            call_llm_fast("prompt")

    @patch("app.llm_config.completion")
    def test_length_finish_reason_is_reported_as_truncation(self, completion_mock: Mock) -> None:
        completion_mock.return_value = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content='{"partial": "response'),
                    finish_reason="length",
                )
            ]
        )

        with self.assertRaises(LLMResponseTruncatedError) as context:
            call_llm_fast("prompt", max_tokens=1234)

        self.assertEqual(context.exception.max_tokens, 1234)
        self.assertEqual(context.exception.finish_reason, "length")
        self.assertEqual(context.exception.response_content, '{"partial": "response')


if __name__ == "__main__":
    unittest.main()
