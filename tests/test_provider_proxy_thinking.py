# tests/test_provider_proxy_thinking.py
import unittest

from gemini_translator.core.worker_helpers.provider_orchestrator import (
    ProviderAttempt,
    _ProviderWorkerProxy,
)


class BaseWorker:
    thinking_enabled = False
    thinking_budget = 0
    thinking_level = "minimal"
    temperature = 1.0
    temperature_override_enabled = True
    worker_id = "w"


def _attempt(**over):
    base = dict(
        provider_id="gemini",
        model_name="m",
        model_config={"id": "m"},
        api_key="k",
        label="content-filter-fallback",
    )
    base.update(over)
    return ProviderAttempt(**base)


class ProxyThinkingTests(unittest.TestCase):
    def test_attempt_thinking_overrides_base(self):
        attempt = _attempt(
            thinking_enabled=True, thinking_budget=2048, thinking_level="HIGH"
        )
        proxy = _ProviderWorkerProxy(BaseWorker(), attempt, {"handler_class": "X"})
        self.assertTrue(proxy.thinking_enabled)
        self.assertEqual(proxy.thinking_budget, 2048)
        self.assertEqual(proxy.thinking_level, "HIGH")

    def test_none_attempt_thinking_falls_back_to_base(self):
        attempt = _attempt()
        proxy = _ProviderWorkerProxy(BaseWorker(), attempt, {"handler_class": "X"})
        self.assertFalse(proxy.thinking_enabled)
        self.assertEqual(proxy.thinking_budget, 0)
        self.assertEqual(proxy.thinking_level, "minimal")


if __name__ == "__main__":
    unittest.main()
