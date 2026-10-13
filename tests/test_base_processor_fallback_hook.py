# tests/test_base_processor_fallback_hook.py
import asyncio
import unittest
from contextlib import contextmanager

from gemini_translator.api.errors import ContentFilterError
from gemini_translator.core.worker_helpers.taskers import base_processor
from gemini_translator.core.worker_helpers.taskers.base_processor import BaseTaskProcessor


class FakeHandler:
    def __init__(self, exc):
        self._exc = exc

    async def execute_api_call(self, prompt, log_prefix, **kwargs):
        raise self._exc


class FakeWorker:
    def __init__(self, *, enabled, exc):
        self.api_handler_instance = FakeHandler(exc)
        self.content_filter_fallback_enabled = enabled
        self.parallel_providers_enabled = False
        self.multi_pass_enabled = False
        self.multi_pass_chapter_translation = False
        self.project_manager = None
        self.output_folder = None
        self.file_path = None

    @contextmanager
    def debug_operation_context(self, ctx):
        yield


def _run(coro):
    return asyncio.run(coro)


class FallbackHookTests(unittest.TestCase):
    def setUp(self):
        self._orig = base_processor.run_content_filter_fallback

    def tearDown(self):
        base_processor.run_content_filter_fallback = self._orig

    def test_content_block_routes_to_fallback_when_enabled(self):
        async def fake_fallback(worker, prompt, log_prefix, **kwargs):
            return "FROM_FALLBACK"

        base_processor.run_content_filter_fallback = fake_fallback
        worker = FakeWorker(enabled=True, exc=ContentFilterError("blocked"))
        proc = BaseTaskProcessor(worker)
        out = _run(
            proc._execute_api_call("P", "[L]", task_info=("t", ("epub_chunk",)), use_stream=False)
        )
        self.assertEqual(out, "FROM_FALLBACK")

    def test_content_block_reraises_when_disabled(self):
        worker = FakeWorker(enabled=False, exc=ContentFilterError("blocked"))
        proc = BaseTaskProcessor(worker)
        with self.assertRaises(ContentFilterError):
            _run(
                proc._execute_api_call("P", "[L]", task_info=("t", ("epub_chunk",)), use_stream=False)
            )


if __name__ == "__main__":
    unittest.main()
