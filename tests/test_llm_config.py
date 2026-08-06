from __future__ import annotations

import unittest
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.config import settings
from app.llm_config import (
    LLMConfigError,
    LLMProfile,
    LLMResponseTruncatedError,
    _resolve_model,
    call_llm_profile,
    call_llm_fast,
)


class LLMConfigTests(unittest.TestCase):
    def test_resolve_model_adds_provider_prefix(self) -> None:
        self.assertEqual(_resolve_model("deepseek", "deepseek-chat"), "deepseek/deepseek-chat")
        self.assertEqual(_resolve_model("ollama_chat", "qwen3.6:35b"), "ollama_chat/qwen3.6:35b")

    @patch("app.llm_config.completion")
    def test_ollama_chat_uses_shared_endpoint_without_api_key(self, completion_mock: Mock) -> None:
        completion_mock.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="answer"))]
        )

        with (
            patch.object(settings, "llm_provider", "ollama_chat"),
            patch.object(settings, "llm_fast_model", "qwen3.6:35b"),
            patch.object(settings, "legacy_llm_api_key", "remote-provider-key"),
            patch.object(settings, "ollama_api_base", "http://127.0.0.1:11434"),
        ):
            result = call_llm_fast("prompt", max_tokens=512)

        self.assertEqual(result, "answer")
        kwargs = completion_mock.call_args.kwargs
        self.assertEqual(kwargs["model"], "ollama_chat/qwen3.6:35b")
        self.assertEqual(kwargs["api_base"], "http://127.0.0.1:11434")
        self.assertNotIn("api_key", kwargs)

    @patch("app.llm_config.completion")
    def test_completion_log_includes_reasoning_setting_without_prompt(self, completion_mock: Mock) -> None:
        completion_mock.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="answer"), finish_reason="stop")]
        )
        prompt = "this prompt must not be logged"
        profile = LLMProfile(
            provider="ollama_chat",
            model="qwen3.6:35b",
            api_key=None,
            api_base="http://127.0.0.1:11434",
            max_tokens=512,
            temperature=0.2,
            reasoning_effort="none",
        )

        with self.assertLogs("app.llm_config", level="INFO") as captured:
            result = call_llm_profile(prompt, profile)

        records = "\n".join(captured.output)
        self.assertEqual(result, "answer")
        self.assertIn("LLM completion started provider=ollama_chat model=qwen3.6:35b", records)
        self.assertIn("reasoning_effort=none", records)
        self.assertIn("LLM completion completed", records)
        self.assertNotIn(prompt, records)

    @patch("app.llm_config.completion")
    def test_fast_call_uses_backend_settings(self, completion_mock: Mock) -> None:
        completion_mock.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="answer"))]
        )

        with (
            patch.object(settings, "llm_provider", "deepseek"),
            patch.object(settings, "llm_fast_model", "deepseek-chat"),
            patch.object(settings, "deepseek_api_key", "test-key"),
            patch.object(settings, "deepseek_api_base", "http://llm.local"),
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
    def test_deepseek_connection_never_inherits_legacy_ollama_base(self, completion_mock: Mock) -> None:
        completion_mock.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="answer"))]
        )

        with (
            patch.object(settings, "llm_provider", "deepseek"),
            patch.object(settings, "deepseek_api_key", "test-key"),
            patch.object(settings, "deepseek_api_base", "https://api.deepseek.com"),
            patch.object(settings, "legacy_llm_api_base", "http://legacy-ollama.invalid:11434"),
        ):
            result = call_llm_fast("prompt")

        self.assertEqual(result, "answer")
        self.assertEqual(completion_mock.call_args.kwargs["api_base"], "https://api.deepseek.com")

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

    def test_ollama_profiles_send_think_and_return_only_final_content(self) -> None:
        requests: list[dict[str, object]] = []

        class OllamaHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - HTTP handler hook name.
                content_length = int(self.headers["Content-Length"])
                requests.append({"path": self.path, "body": json.loads(self.rfile.read(content_length))})
                response = {
                    "model": "qwen3.6:35b",
                    "created_at": "2026-08-04T00:00:00Z",
                    "message": {
                        "role": "assistant",
                        "thinking": "private reasoning must not be persisted",
                        "content": "final answer only",
                    },
                    "done": True,
                    "done_reason": "stop",
                }
                body = json.dumps(response).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), OllamaHandler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        api_base = f"http://127.0.0.1:{server.server_port}"
        try:
            direct = call_llm_profile(
                "answer directly",
                LLMProfile(
                    provider="ollama_chat",
                    model="qwen3.6:35b",
                    api_key=None,
                    api_base=api_base,
                    max_tokens=512,
                    temperature=0.2,
                    reasoning_effort="none",
                ),
            )
            thinking = call_llm_profile(
                "answer after thinking",
                LLMProfile(
                    provider="ollama_chat",
                    model="qwen3.6:35b",
                    api_key=None,
                    api_base=api_base,
                    max_tokens=512,
                    temperature=0.2,
                    reasoning_effort="low",
                ),
            )
        finally:
            server.shutdown()
            server.server_close()

        self.assertEqual(direct, "final answer only")
        self.assertEqual(thinking, "final answer only")
        chat_requests = [request["body"] for request in requests if request["path"] == "/api/chat"]
        self.assertGreaterEqual(len(chat_requests), 2)
        self.assertTrue(any(request["think"] is False for request in chat_requests))
        self.assertTrue(any(request["think"] is True for request in chat_requests))
        self.assertNotIn("thinking", direct)


if __name__ == "__main__":
    unittest.main()
