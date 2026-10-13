import errno
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtCore, QtWidgets

from gemini_translator.utils.settings import SettingsManager


class _RecordingBus(QtCore.QObject):
    event_posted = QtCore.pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self.events = []
        self.event_posted.connect(self.events.append)


class SettingsSaveFailureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_delayed_save_keeps_dirty_state_when_disk_is_full(self):
        bus = _RecordingBus()

        with tempfile.TemporaryDirectory() as tmpdir:
            manager = SettingsManager(
                event_bus=bus,
                config_file=os.path.join(tmpdir, "settings.json"),
            )
            manager._cache = {"custom_prompt": "changed"}
            manager._is_dirty = True
            disk_full = OSError(errno.ENOSPC, "No space left on device")

            with patch("builtins.open", side_effect=disk_full):
                manager._perform_save()

        self.assertTrue(manager._is_dirty)
        self.assertEqual(manager._last_save_error, disk_full)
        self.assertEqual(bus.events[-1]["event"], "settings_save_failed")
        self.assertEqual(bus.events[-1]["data"]["errno"], errno.ENOSPC)


if __name__ == "__main__":
    unittest.main()
