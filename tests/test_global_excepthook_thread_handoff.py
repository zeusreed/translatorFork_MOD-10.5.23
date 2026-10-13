import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtCore, QtWidgets

import main


class _SignalStub:
    def __init__(self):
        self.messages = []

    def emit(self, message):
        self.messages.append(message)


class GlobalExcepthookThreadHandoffTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_excepthook_uses_application_dispatcher_instead_of_thread_local_timer(self):
        signal = _SignalStub()
        fake_app = type("FakeApp", (), {"critical_error_requested": signal})()

        with patch.object(QtWidgets.QApplication, "instance", return_value=fake_app), \
                patch.object(QtCore.QTimer, "singleShot") as single_shot:
            try:
                raise RuntimeError("background boom")
            except RuntimeError as exc:
                main.global_excepthook(type(exc), exc, exc.__traceback__)

        self.assertEqual(len(signal.messages), 1)
        self.assertIn("background boom", signal.messages[0])
        single_shot.assert_not_called()


if __name__ == "__main__":
    unittest.main()
