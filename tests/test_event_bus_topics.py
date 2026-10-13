import os
import threading
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtCore, QtWidgets

from main import EventBus


class EventBusTopicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_emit_event_delivers_to_topic_and_legacy_subscribers(self):
        bus = EventBus()
        topic_events = []
        legacy_events = []

        bus.subscribe("log_message", topic_events.append)
        bus.event_posted.connect(legacy_events.append)

        event = {"event": "log_message", "data": {"message": "hello"}}
        bus.emit_event(event)

        self.assertEqual(topic_events, [event])
        self.assertEqual(legacy_events, [event])

    def test_topic_subscriber_only_receives_matching_event(self):
        bus = EventBus()
        topic_events = []

        bus.subscribe("log_message", topic_events.append)

        bus.emit_event({"event": "task_state_changed", "data": {}})

        self.assertEqual(topic_events, [])

    def test_unsubscribe_removes_topic_callback(self):
        bus = EventBus()
        topic_events = []

        bus.subscribe("log_message", topic_events.append)
        bus.unsubscribe("log_message", topic_events.append)

        bus.emit_event({"event": "log_message", "data": {"message": "hello"}})

        self.assertEqual(topic_events, [])

    def test_unsubscribe_all_removes_callback_from_every_topic(self):
        bus = EventBus()
        topic_events = []

        bus.subscribe("log_message", topic_events.append)
        bus.subscribe("session_finished", topic_events.append)
        bus.unsubscribe_all(topic_events.append)

        bus.emit_event({"event": "log_message", "data": {}})
        bus.emit_event({"event": "session_finished", "data": {}})

        self.assertEqual(topic_events, [])

    def test_subscribe_from_foreign_thread_does_not_create_qt_child_in_wrong_thread(self):
        bus = EventBus()
        topic_events = []
        messages = []
        errors = []

        previous_handler = QtCore.qInstallMessageHandler(
            lambda mode, context, message: messages.append(message)
        )
        try:
            def subscribe_from_thread():
                try:
                    bus.subscribe("log_message", topic_events.append)
                except Exception as exc:
                    errors.append(exc)

            thread = threading.Thread(target=subscribe_from_thread)
            thread.start()
            deadline = time.monotonic() + 2
            while thread.is_alive() and time.monotonic() < deadline:
                self.app.processEvents()
                thread.join(timeout=0.01)
        finally:
            QtCore.qInstallMessageHandler(previous_handler)

        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        cross_thread_warnings = [
            message for message in messages
            if "Cannot create children for a parent that is in a different thread" in message
        ]
        self.assertEqual(cross_thread_warnings, [])

        event = {"event": "log_message", "data": {"message": "hello"}}
        bus.emit_event(event)

        self.assertEqual(topic_events, [event])

    def test_subscribe_from_foreign_thread_does_not_require_gui_event_pump(self):
        bus = EventBus()
        topic_events = []
        errors = []

        def subscribe_from_thread():
            try:
                bus.subscribe("log_message", topic_events.append)
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=subscribe_from_thread)
        try:
            thread.start()
            thread.join(timeout=0.2)
            self.assertFalse(thread.is_alive())
        finally:
            deadline = time.monotonic() + 2
            while thread.is_alive() and time.monotonic() < deadline:
                self.app.processEvents()
                thread.join(timeout=0.01)

        self.assertEqual(errors, [])

        event = {"event": "log_message", "data": {"message": "hello"}}
        bus.emit_event(event)

        self.assertEqual(topic_events, [event])

    def test_worker_logs_are_buffered_and_drained_in_bounded_batches(self):
        bus = EventBus()
        bus.LOG_EVENT_BATCH_SIZE = 3
        bus.LOG_EVENT_DRAIN_INTERVAL_MS = 1000
        topic_events = []
        bus.subscribe("log_message", topic_events.append)

        def emit_logs():
            for index in range(10):
                bus.emit_event({
                    "event": "log_message",
                    "data": {"message": f"message-{index}"},
                })

        thread = threading.Thread(target=emit_logs)
        thread.start()
        thread.join(timeout=1)

        self.assertFalse(thread.is_alive())
        self.assertEqual(topic_events, [])
        self.assertEqual(len(bus._pending_log_events), 10)

        bus._drain_pending_log_events()

        self.assertEqual(len(topic_events), 3)
        self.assertEqual(len(bus._pending_log_events), 7)

        # Leave no delayed work behind for the shared QApplication. The timer
        # already scheduled by the drain becomes a harmless no-op.
        with bus._pending_log_lock:
            bus._pending_log_events.clear()
            bus._log_drain_scheduled = False

    def test_worker_log_buffer_is_bounded_and_reports_drops(self):
        bus = EventBus()
        bus.MAX_PENDING_LOG_EVENTS = 3
        bus.LOG_EVENT_BATCH_SIZE = 10
        topic_events = []
        bus.subscribe("log_message", topic_events.append)

        def emit_logs():
            for index in range(5):
                bus.emit_event({
                    "event": "log_message",
                    "data": {"message": f"message-{index}"},
                })

        thread = threading.Thread(target=emit_logs)
        thread.start()
        thread.join(timeout=1)
        bus._drain_pending_log_events()

        messages = [event["data"]["message"] for event in topic_events]
        self.assertIn("Пропущено 2 сообщений лога", messages[0])
        self.assertEqual(messages[1:], ["message-2", "message-3", "message-4"])


if __name__ == "__main__":
    unittest.main()
