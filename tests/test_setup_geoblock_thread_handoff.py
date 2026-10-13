import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtCore

from gemini_translator.ui.dialogs.setup import InitialSetupDialog


class _GeoblockHarness:
    on_event = InitialSetupDialog.on_event

    def __init__(self):
        self.is_blocked_by_child_dialog = False
        self.is_session_active = True
        self.dialog_calls = 0

    def _handle_geoblock_detected(self):
        self.dialog_calls += 1


class SetupGeoblockThreadHandoffTests(unittest.TestCase):
    def test_geoblock_event_queues_dialog_instead_of_showing_synchronously(self):
        harness = _GeoblockHarness()
        invoke_calls = []

        def fake_invoke(receiver, method_name, connection_type):
            invoke_calls.append((receiver, method_name, connection_type))
            return True

        with patch.object(QtCore.QMetaObject, "invokeMethod", side_effect=fake_invoke):
            harness.on_event({"event": "geoblock_detected", "data": {}})

        self.assertEqual(harness.dialog_calls, 0)
        self.assertEqual(len(invoke_calls), 1)
        receiver, method_name, connection_type = invoke_calls[0]
        self.assertIs(receiver, harness)
        self.assertEqual(method_name, "_handle_geoblock_detected")
        self.assertEqual(connection_type, QtCore.Qt.ConnectionType.QueuedConnection)


if __name__ == "__main__":
    unittest.main()
