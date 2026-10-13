import types
import unittest

from gemini_translator.api.base import BaseApiHandler


def _handler(is_cancelled, is_shutting_down):
    handler = BaseApiHandler.__new__(BaseApiHandler)
    handler.worker = types.SimpleNamespace(
        is_cancelled=is_cancelled,
        is_shutting_down=is_shutting_down,
    )
    return handler


class ShutdownCancellationTests(unittest.TestCase):
    """On stop, the runtime cancels in-flight coroutines (stop_workers); the api
    layer must classify that as a user CANCEL (skipped by the error analyzer),
    not a NETWORK error that gets logged and requeued."""

    def test_explicit_cancel_is_shutdown(self):
        self.assertTrue(_handler(is_cancelled=True, is_shutting_down=False)._is_shutdown_cancellation())

    def test_graceful_shutdown_is_shutdown(self):
        # The regression: during graceful shutdown the runtime cancels in-flight
        # coroutines while the worker's is_cancelled is still False. is_shutting_down
        # must count, otherwise the cancellation is mislabelled NETWORK.
        self.assertTrue(_handler(is_cancelled=False, is_shutting_down=True)._is_shutdown_cancellation())

    def test_neither_flag_is_a_real_network_interruption(self):
        # A CancelledError with no stop signal is a genuine network-level interrupt
        # (DNS timeout / connection reset) and must stay classified as NETWORK.
        self.assertFalse(_handler(is_cancelled=False, is_shutting_down=False)._is_shutdown_cancellation())


if __name__ == "__main__":
    unittest.main()
