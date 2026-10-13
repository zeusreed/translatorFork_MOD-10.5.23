import asyncio
import unittest

from gemini_translator.api.base import BaseApiHandler


class _SettingsStub:
    def increment_request_count(self, *args, **kwargs):
        return True

    def decrement_request_count(self, *args, **kwargs):
        return True


class _AsyncWorkerStub:
    def __init__(self):
        self.provider_config = {"is_async": True, "base_timeout": 60}
        self.settings_manager = _SettingsStub()
        self.api_key = "test-key"
        self.model_id = "test-model"
        self.is_cancelled = False
        self.debug_logging_enabled = False


class ApiExecutorShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_executor_cancels_owned_tasks_when_parent_is_cancelled(self):
        class HangingAsyncHandler(BaseApiHandler):
            def __init__(self, worker):
                super().__init__(worker)
                self.call_started = asyncio.Event()
                self.call_cancelled = False
                self.call_task = None

            async def call_api(self, *args, **kwargs):
                self.call_task = asyncio.current_task()
                self.call_started.set()
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    self.call_cancelled = True
                    raise

        handler = HangingAsyncHandler(_AsyncWorkerStub())
        executor_task = asyncio.create_task(
            handler._async_executor(
                "prompt",
                "[test]",
                False,
                True,
                False,
                None,
            )
        )

        await asyncio.wait_for(handler.call_started.wait(), timeout=1)
        executor_task.cancel()

        try:
            with self.assertRaises(asyncio.CancelledError):
                await executor_task
            await asyncio.sleep(0)
            self.assertTrue(handler.call_cancelled)
        finally:
            cleanup_targets = [
                task
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task()
                and (
                    task is handler.call_task
                    or getattr(task.get_coro(), "__qualname__", "").endswith("_cancellation_checker")
                    or getattr(task.get_coro(), "__name__", "") == "wait_for"
                )
            ]
            for task in cleanup_targets:
                task.cancel()
            if cleanup_targets:
                await asyncio.gather(*cleanup_targets, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
