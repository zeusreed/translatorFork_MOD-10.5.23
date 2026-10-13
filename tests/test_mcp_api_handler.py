import asyncio
import threading

import pytest

from gemini_translator.api import config as api_config
from gemini_translator.api.errors import (
    NetworkError,
    RateLimitExceededError,
    TemporaryRateLimitError,
    ValidationFailedError,
)
from gemini_translator.api.handlers.mcp import McpApiHandler
from gemini_translator.api.factory import get_api_handler_class


class FakeSettingsManager:
    def __init__(self):
        self.incremented = []
        self.decremented = []

    def increment_request_count(self, api_key, model_id):
        self.incremented.append((api_key, model_id))

    def decrement_request_count(self, api_key, model_id):
        self.decremented.append((api_key, model_id))


class FakePromptBuilder:
    system_instruction = "SYSTEM"


class FakeWorker:
    def __init__(self):
        self.provider_config = {"base_timeout": 20}
        self.model_config = {"id": "mcp-client", "provider": "__mcp_server__"}
        self.settings_manager = FakeSettingsManager()
        self.api_key = "__mcp_client_session__"
        self.model_id = "mcp-client"
        self.prompt_builder = FakePromptBuilder()
        self.temperature_override_enabled = True
        self.temperature = 0.35
        self.debug_logging_enabled = False
        self.debug_operation_filters = None
        self.debug_max_log_mb = 128
        self.is_cancelled = False
        self.is_shutting_down = False
        self.sync_executor = None
        self.events = []

    def _post_event(self, event_name, payload):
        self.events.append((event_name, payload))

    def get_debug_operation_context(self):
        return {"surface": "unit-test"}


class FakeDaemonClient:
    def __init__(self, response):
        self.response = response
        self.payloads = []
        self.timeouts = []

    def request_ai_completion(self, payload, timeout=None):
        self.payloads.append(payload)
        self.timeouts.append(timeout)
        return self.response


class BlockingDaemonClient:
    def __init__(self):
        self.started = threading.Event()
        self.released = threading.Event()
        self.payload = None
        self.cancelled_request_ids = []

    def request_ai_completion(self, payload, timeout=None):
        self.payload = payload
        self.started.set()
        self.released.wait(timeout=1)
        return {"ok": True, "text": "late"}

    def cancel_ai_completion(self, request_id):
        self.cancelled_request_ids.append(str(request_id))
        self.released.set()
        return {"ok": True, "cancelled": True}


def test_mcp_api_handler_returns_daemon_text_without_incrementing_key_counter(monkeypatch):
    worker = FakeWorker()
    client = FakeDaemonClient({"ok": True, "text": "ответ"})
    monkeypatch.setattr("gemini_translator.api.handlers.mcp.load_client", lambda state_dir=None: client)

    result = asyncio.run(
        McpApiHandler(worker).execute_api_call(
            "PROMPT",
            "[TEST]",
            max_output_tokens=1234,
        )
    )

    assert result == "ответ"
    assert worker.settings_manager.incremented == []
    request_payload = dict(client.payloads[0])
    assert request_payload.pop("request_id")
    assert request_payload == {
        "prompt": "PROMPT",
        "system_instruction": "SYSTEM",
        "max_output_tokens": 1234,
        "temperature": 0.35,
        "timeout_sec": 20,
        "metadata": {
            "log_prefix": "[TEST]",
            "model_id": "mcp-client",
            "operation_context": {"surface": "unit-test"},
        },
    }
    assert client.timeouts == [25]


def test_mcp_api_handler_rejects_empty_daemon_text(monkeypatch):
    worker = FakeWorker()
    client = FakeDaemonClient({"ok": True, "text": "   "})
    monkeypatch.setattr("gemini_translator.api.handlers.mcp.load_client", lambda state_dir=None: client)

    with pytest.raises(ValidationFailedError):
        asyncio.run(McpApiHandler(worker).execute_api_call("PROMPT", "[TEST]"))


def test_mcp_api_handler_maps_daemon_error_to_network_error(monkeypatch):
    worker = FakeWorker()
    client = FakeDaemonClient({"ok": False, "error": "no MCP client is connected"})
    monkeypatch.setattr("gemini_translator.api.handlers.mcp.load_client", lambda state_dir=None: client)

    with pytest.raises(NetworkError, match="no MCP client is connected"):
        asyncio.run(McpApiHandler(worker).execute_api_call("PROMPT", "[TEST]"))


def test_mcp_api_handler_maps_short_ai_limit_to_temporary_limit(monkeypatch):
    worker = FakeWorker()
    client = FakeDaemonClient(
        {
            "ok": False,
            "error": "rate limit exceeded; try again in 90 seconds",
            "retry_after_seconds": 90,
        }
    )
    monkeypatch.setattr("gemini_translator.api.handlers.mcp.load_client", lambda state_dir=None: client)

    with pytest.raises(TemporaryRateLimitError) as raised:
        asyncio.run(McpApiHandler(worker).execute_api_call("PROMPT", "[TEST]"))

    assert raised.value.delay_seconds == 90
    assert "Сброс" in str(raised.value)


def test_mcp_api_handler_finishes_session_for_five_hour_window_even_near_reset(monkeypatch):
    worker = FakeWorker()
    client = FakeDaemonClient(
        {
            "ok": False,
            "error": "5-hour usage limit reached. Limit resets in 2 minutes.",
            "reset_after_seconds": 120,
        }
    )
    monkeypatch.setattr("gemini_translator.api.handlers.mcp.load_client", lambda state_dir=None: client)

    with pytest.raises(RateLimitExceededError) as raised:
        asyncio.run(McpApiHandler(worker).execute_api_call("PROMPT", "[TEST]"))

    assert "5-час" in str(raised.value)
    assert "Сброс" in str(raised.value)
    assert getattr(raised.value, "mcp_is_long_window") is True


def test_mcp_api_handler_finishes_session_for_weekly_window_even_near_reset(monkeypatch):
    worker = FakeWorker()
    client = FakeDaemonClient(
        {
            "ok": False,
            "error": "Weekly usage limit reached; resets in 2 minutes.",
            "reset_after_seconds": 120,
        }
    )
    monkeypatch.setattr("gemini_translator.api.handlers.mcp.load_client", lambda state_dir=None: client)

    with pytest.raises(RateLimitExceededError) as raised:
        asyncio.run(McpApiHandler(worker).execute_api_call("PROMPT", "[TEST]"))

    assert "недель" in str(raised.value).lower()
    assert "Сброс" in str(raised.value)


def test_mcp_api_handler_classifies_daemon_client_limit_error(monkeypatch):
    worker = FakeWorker()

    def raise_limit(state_dir=None):
        from gemini_translator.mcp.client import DaemonClientError

        raise DaemonClientError("429 rate limit exceeded; retry after 60 seconds")

    monkeypatch.setattr("gemini_translator.api.handlers.mcp.load_client", raise_limit)

    with pytest.raises(TemporaryRateLimitError) as raised:
        asyncio.run(McpApiHandler(worker).execute_api_call("PROMPT", "[TEST]"))

    assert raised.value.delay_seconds == 60


def test_mcp_api_handler_cancels_daemon_completion_when_task_is_cancelled(monkeypatch):
    worker = FakeWorker()
    client = BlockingDaemonClient()
    monkeypatch.setattr("gemini_translator.api.handlers.mcp.load_client", lambda state_dir=None: client)

    async def run_and_cancel():
        task = asyncio.create_task(McpApiHandler(worker).execute_api_call("PROMPT", "[TEST]"))
        assert await asyncio.to_thread(client.started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run_and_cancel())

    assert client.payload["request_id"]
    assert client.cancelled_request_ids == [client.payload["request_id"]]


def test_mcp_api_handler_uses_daemon_client_error_payload_for_long_window(monkeypatch):
    worker = FakeWorker()

    def raise_limit(state_dir=None):
        from gemini_translator.mcp.client import DaemonClientError

        raise DaemonClientError(
            "usage limit reached",
            payload={
                "error": "usage limit reached",
                "reset_after_seconds": 120,
                "limit_window_seconds": 5 * 60 * 60,
            },
        )

    monkeypatch.setattr("gemini_translator.api.handlers.mcp.load_client", raise_limit)

    with pytest.raises(RateLimitExceededError) as raised:
        asyncio.run(McpApiHandler(worker).execute_api_call("PROMPT", "[TEST]"))

    assert "длинное окно" in str(raised.value)
    assert "Сброс" in str(raised.value)


def test_mcp_api_handler_uses_model_default_max_output_tokens(monkeypatch):
    worker = FakeWorker()
    worker.model_config["max_output_tokens"] = 4321
    client = FakeDaemonClient({"ok": True, "text": "ответ"})
    monkeypatch.setattr("gemini_translator.api.handlers.mcp.load_client", lambda state_dir=None: client)

    asyncio.run(McpApiHandler(worker).execute_api_call("PROMPT", "[TEST]"))

    assert client.payloads[0]["max_output_tokens"] == 4321


def test_mcp_provider_config_is_hidden_virtual_provider():
    api_config.initialize_configs()

    provider = api_config.api_providers()["__mcp_server__"]
    model = api_config.all_models()["MCP Client"]

    assert provider["visible"] is False
    assert provider["requires_api_key"] is False
    assert api_config.provider_placeholder_api_key("__mcp_server__") == "__mcp_client_session__"
    assert get_api_handler_class(provider["handler_class"]) is McpApiHandler
    assert model["provider"] == "__mcp_server__"
    assert model["id"] == "mcp-client"
