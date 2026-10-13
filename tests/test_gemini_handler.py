import unittest
from types import SimpleNamespace

from gemini_translator.api.errors import TemporaryRateLimitError
from gemini_translator.api.handlers.gemini import GeminiApiHandler


def _make_handler() -> GeminiApiHandler:
    worker = SimpleNamespace(
        provider_config={"is_async": True},
        model_config={"id": "gemini-test"},
        api_key="test-api-key",
    )
    return GeminiApiHandler(worker)


class GeminiHandlerTests(unittest.TestCase):
    def test_stream_unavailable_high_demand_is_temporary_rate_limit(self):
        handler = _make_handler()
        stream_error = {
            "message": (
                "This model is currently experiencing high demand. "
                "Spikes in demand are usually temporary. Please try again later."
            ),
            "status": "UNAVAILABLE",
        }

        with self.assertRaises(TemporaryRateLimitError) as raised:
            handler._raise_for_stream_error(stream_error)

        self.assertEqual(raised.exception.delay_seconds, 20)
        self.assertIn("перегружена", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
