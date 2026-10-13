import os
import asyncio
import tempfile
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import gemini_reader_v3 as reader
from gemini_reader_v3 import BookManager, GeminiWorker, MainWindow, Qt, ReaderWorkerStopped, _to_thread_with_timeout


class _SignalSpy:
    def __init__(self):
        self.calls = []

    def emit(self, *args):
        self.calls.append(args)


class _WorkerProgressHarness:
    _emit_worker_progress = GeminiWorker._emit_worker_progress

    def __init__(self):
        self.worker_id = 7
        self.worker_progress = _SignalSpy()
        self._last_progress_emit_payload = None
        self._last_progress_emit_at = 0.0


class _WorkerCrashHarness:
    run = GeminiWorker.run
    _emit_finished = GeminiWorker._emit_finished

    def __init__(self):
        self.worker_id = 9
        self.error_signal = _SignalSpy()
        self.finished_signal = _SignalSpy()
        self._is_running = True
        self._finished_emitted = False

    async def main_loop(self):
        raise RuntimeError("boom")


class _LiveAutosaveHarness:
    _should_autosave_live_mp3 = GeminiWorker._should_autosave_live_mp3

    def __init__(self):
        self.record = True
        self.c_idx = 0
        self.s_idx = 0
        self._last_live_mp3_autosave_step = 0
        self._last_live_mp3_autosave_at = 0.0


class _FakeChapterItem:
    def __init__(self, idx, check_state):
        self._idx = idx
        self._check_state = check_state

    def data(self, role):
        if role == Qt.ItemDataRole.UserRole:
            return self._idx
        return None

    def checkState(self):
        return self._check_state


class _ChapterCheckHarness:
    _checked_chapter_indices = MainWindow._checked_chapter_indices
    _on_chapter_item_changed = MainWindow._on_chapter_item_changed

    def __init__(self):
        self.bm = type("Book", (), {"chapters": [object() for _ in range(5)]})()
        self._chapter_check_state_refresh = False
        self._loading_settings = False
        self._checked_chapter_indices_state = set()
        self._chapter_check_anchor_index = None
        self._chapter_last_press_index = None
        self._chapter_last_press_modifiers = Qt.KeyboardModifier.NoModifier
        self.scope_refreshes = 0
        self.settings_saves = 0
        self.range_calls = []

    def _apply_checked_range(self, anchor_idx, current_idx, desired_state):
        self.range_calls.append((anchor_idx, current_idx, desired_state))

    def _schedule_chapter_scope_refresh(self):
        self.scope_refreshes += 1

    def _schedule_save_settings(self):
        self.settings_saves += 1


class ReaderProgressThrottleTests(unittest.TestCase):
    def test_duplicate_progress_payload_is_suppressed(self):
        harness = _WorkerProgressHarness()

        harness._emit_worker_progress(3, 10, 100)
        harness._emit_worker_progress(3, 10, 100)

        self.assertEqual(len(harness.worker_progress.calls), 1)
        self.assertEqual(harness.worker_progress.calls[0], (7, 3, 10, 100))

    def test_force_progress_bypasses_throttle(self):
        harness = _WorkerProgressHarness()

        harness._emit_worker_progress(3, 100, 100)
        harness._emit_worker_progress(3, 100, 100, force=True)

        self.assertEqual(len(harness.worker_progress.calls), 2)

    def test_worker_crash_still_emits_finished_once(self):
        harness = _WorkerCrashHarness()

        harness.run()
        harness._emit_finished()

        self.assertEqual(len(harness.error_signal.calls), 1)
        self.assertIn("CRASH: boom", harness.error_signal.calls[0][1])
        self.assertEqual(harness.finished_signal.calls, [(9,)])

    def test_blocking_helper_times_out_without_waiting_for_thread(self):
        started = time.monotonic()

        with self.assertRaises(RuntimeError) as ctx:
            asyncio.run(
                _to_thread_with_timeout(
                    "slow helper",
                    0.1,
                    time.sleep,
                    2,
                    poll_interval=0.02,
                )
            )

        self.assertIn("timed out", str(ctx.exception))
        self.assertLess(time.monotonic() - started, 1.0)

    def test_blocking_helper_obeys_stop_callback(self):
        started = time.monotonic()

        with self.assertRaises(ReaderWorkerStopped):
            asyncio.run(
                _to_thread_with_timeout(
                    "stoppable helper",
                    5,
                    time.sleep,
                    2,
                    should_continue=lambda: False,
                    poll_interval=0.02,
                )
            )

        self.assertLess(time.monotonic() - started, 1.0)

    def test_progress_save_throttles_immediate_disk_writes_until_forced(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = BookManager()
            manager.book_dir = temp_dir

            self.assertTrue(manager.save_progress(0, 1))
            self.assertFalse(manager.save_progress(0, 2))
            self.assertEqual(manager.load_progress(), (0, 1))

            self.assertTrue(manager.save_progress(0, 2, force=True))
            self.assertEqual(manager.load_progress(), (0, 2))

    def test_live_mp3_autosave_waits_for_interval_and_non_final_step(self):
        harness = _LiveAutosaveHarness()
        harness._last_live_mp3_autosave_at = time.monotonic() - 100

        harness.s_idx = 4
        self.assertFalse(harness._should_autosave_live_mp3(100))

        harness.s_idx = 5
        self.assertTrue(harness._should_autosave_live_mp3(100))

        harness.s_idx = 100
        self.assertFalse(harness._should_autosave_live_mp3(100))

    def test_chapter_checkbox_change_updates_cached_state_without_full_scan(self):
        app = reader.QApplication.instance() or reader.QApplication([])
        self.assertIsNotNone(app)
        harness = _ChapterCheckHarness()

        harness._on_chapter_item_changed(_FakeChapterItem(2, Qt.CheckState.Checked))

        self.assertEqual(harness._checked_chapter_indices(), [2])
        self.assertEqual(harness.scope_refreshes, 1)
        self.assertEqual(harness.settings_saves, 1)

        harness._on_chapter_item_changed(_FakeChapterItem(2, Qt.CheckState.Unchecked))

        self.assertEqual(harness._checked_chapter_indices(), [])

    def test_shift_checkbox_change_uses_cached_anchor_range(self):
        app = reader.QApplication.instance() or reader.QApplication([])
        self.assertIsNotNone(app)
        harness = _ChapterCheckHarness()
        harness._chapter_check_anchor_index = 1
        harness._chapter_last_press_modifiers = Qt.KeyboardModifier.ShiftModifier

        harness._on_chapter_item_changed(_FakeChapterItem(3, Qt.CheckState.Checked))

        self.assertEqual(harness.range_calls, [(1, 3, Qt.CheckState.Checked)])


if __name__ == "__main__":
    unittest.main()
