import asyncio
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from gemini_translator.api.errors import NetworkError
from gemini_translator.core.worker_helpers import gemini_model_fallback as fallback


class _Worker:
    api_provider_name = "gemini"
    api_key = "secret"
    model_id = "gemini-primary"
    model_config = {"id": "gemini-primary"}
    gemini_model_fallback_enabled = True
    gemini_fallback_model_id = "gemini-2.5-flash-lite"

    @contextmanager
    def debug_operation_context(self, _context):
        yield


class GeminiModelFallbackTests(unittest.TestCase):
    def test_setting_can_disable_fallback(self):
        worker = _Worker()
        worker.gemini_model_fallback_enabled = False
        self.assertFalse(fallback.should_try_gemini_model_fallback(worker, NetworkError("offline")))

    def test_retries_same_prompt_using_selected_model(self):
        worker = _Worker()
        prompt = "same chunk text"
        captured = {}

        async def fake_run_attempt(_worker, attempt, actual_prompt, log_prefix, call_kwargs):
            captured.update(
                model_id=attempt.model_config["id"],
                prompt=actual_prompt,
                log_prefix=log_prefix,
                call_kwargs=call_kwargs,
            )
            return SimpleNamespace(ok=True, text="translated", exception=None)

        with (
            patch.object(
                fallback,
                "_resolve_model",
                return_value=("Gemini 2.5 Flash-Lite", {"id": "gemini-2.5-flash-lite"}),
            ),
            patch.object(fallback, "_run_attempt", side_effect=fake_run_attempt),
            patch.object(fallback, "_post_log"),
        ):
            result = asyncio.run(
                fallback.retry_with_older_gemini_model(
                    worker,
                    prompt,
                    "chapter 1",
                    {"use_stream": False},
                    NetworkError("primary offline"),
                )
            )

        self.assertEqual(result, "translated")
        self.assertEqual(captured["model_id"], "gemini-2.5-flash-lite")
        self.assertEqual(captured["prompt"], prompt)
        self.assertEqual(captured["log_prefix"], "chapter 1")
        self.assertEqual(captured["call_kwargs"], {"use_stream": False})


if __name__ == "__main__":
    unittest.main()
