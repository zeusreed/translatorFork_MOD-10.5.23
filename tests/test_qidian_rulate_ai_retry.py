from gemini_translator.api.errors import TemporaryRateLimitError
from qidian_rulate import workers


class _SettingsManager:
    def __init__(self):
        self.increments = 0
        self.decrements = 0

    def increment_request_count(self, api_key, model_id):
        self.increments += 1

    def decrement_request_count(self, api_key, model_id):
        self.decrements += 1

    def load_proxy_settings(self):
        return None


class _RetryOnceHandler:
    calls = 0

    def __init__(self, worker):
        self.worker = worker

    def setup_client(self, client_override=None, proxy_settings=None):
        return True

    async def execute_api_call(self, *args, **kwargs):
        type(self).calls += 1
        if type(self).calls == 1:
            raise TemporaryRateLimitError("temporary overload", delay_seconds=30)
        return "ok"


def test_qidian_ai_request_retries_temporary_rate_limit(monkeypatch):
    _RetryOnceHandler.calls = 0
    monkeypatch.setattr(
        workers.api_config,
        "api_providers",
        lambda: {"gemini": {"handler_class": "RetryOnceHandler", "is_async": True}},
    )
    monkeypatch.setattr(
        workers.api_config,
        "all_models",
        lambda: {"gemini-test": {"id": "gemini-test"}},
    )
    monkeypatch.setattr(workers.api_config, "default_model_name", lambda: "gemini-test")
    monkeypatch.setattr(workers, "get_api_handler_class", lambda name: _RetryOnceHandler)
    monkeypatch.setattr(workers.time, "sleep", lambda seconds: None)
    logs = []

    result = workers._run_ai_request(
        provider_id="gemini",
        model_settings={"model": "gemini-test"},
        active_keys=["test-key"],
        settings_manager=_SettingsManager(),
        prompt="prompt",
        log_callback=lambda level, message: logs.append((level, message)),
        log_prefix="Qidian -> Rulate catalog",
    )

    assert result == "ok"
    assert _RetryOnceHandler.calls == 2
    assert any("повтор" in message for _, message in logs)
