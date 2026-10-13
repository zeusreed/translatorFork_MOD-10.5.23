"""Прозрачное zstd-сжатие блобов chunk_results.translated_content.

Требования:
- API не меняется: task_done_with_content принимает текст,
  get_completed_previous_chunk_translation возвращает текст;
- в самой БД лежит zstd-кадр (bytes с magic 0x28B52FFD), заметно короче текста;
- легаси-строки (старые снапшоты сессий, прямые вставки в тестах) читаются
  как раньше — формат определяется по magic, не по версии схемы.
"""

import os
import sqlite3
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtCore, QtWidgets

from gemini_translator.api import config as api_config
from gemini_translator.core.task_manager import ChapterQueueManager, decompress_chunk_content


class _DummyBus(QtCore.QObject):
    event_posted = QtCore.pyqtSignal(dict)


CHUNK_TEXT = "<body>" + "<p>перевод первого чанка главы 森羅</p>" * 200 + "</body>"


class ChunkContentCompressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        cls.app.event_bus = _DummyBus()
        cls.app.main_db_connection = sqlite3.connect(
            api_config.SHARED_DB_URI,
            uri=True,
            check_same_thread=False,
        )
        cls.app.main_db_connection.row_factory = sqlite3.Row

    def _make_manager(self):
        manager = ChapterQueueManager(event_bus=self.app.event_bus)
        self.addCleanup(manager.clear_all_queues)
        manager.clear_all_queues()
        with manager._get_write_conn() as conn:
            conn.execute("DELETE FROM chunk_results")
        return manager

    def _complete_first_chunk(self, manager, text):
        chain = [
            ("epub_chunk", "book.epub", "Text/zstd.xhtml", "<p>s1</p>", 0, 2, "", ""),
            ("epub_chunk", "book.epub", "Text/zstd.xhtml", "<p>s2</p>", 1, 2, "", ""),
        ]
        manager.set_pending_task_chains([chain])
        first = manager.get_next_task("worker-z")
        manager.task_done_with_content("worker-z", first, text, "gemini")
        return manager.get_next_task("worker-z")

    def test_roundtrip_returns_original_text(self):
        manager = self._make_manager()
        second = self._complete_first_chunk(manager, CHUNK_TEXT)
        self.assertEqual(
            manager.get_completed_previous_chunk_translation(second[0], "Text/zstd.xhtml", 1),
            CHUNK_TEXT,
        )

    def test_storage_is_zstd_compressed(self):
        manager = self._make_manager()
        self._complete_first_chunk(manager, CHUNK_TEXT)
        with manager._light_read_conn() as conn:
            raw = conn.execute(
                "SELECT translated_content FROM chunk_results"
            ).fetchone()["translated_content"]
        self.assertIsInstance(raw, bytes)
        self.assertEqual(raw[:4], b"\x28\xb5\x2f\xfd")
        self.assertLess(len(raw), len(CHUNK_TEXT.encode("utf-8")) // 4)

    def test_legacy_plain_text_rows_still_readable(self):
        manager = self._make_manager()
        second = self._complete_first_chunk(manager, CHUNK_TEXT)
        # Подменяем блоб на легаси-текст, как в старом снапшоте сессии
        with manager._get_write_conn() as conn:
            conn.execute(
                "UPDATE chunk_results SET translated_content = ?",
                ("легаси перевод без сжатия",),
            )
        self.assertEqual(
            manager.get_completed_previous_chunk_translation(second[0], "Text/zstd.xhtml", 1),
            "легаси перевод без сжатия",
        )

    def test_stale_worker_cannot_overwrite_reclaimed_chunk_result(self):
        manager = self._make_manager()
        payload = ("epub_chunk", "book.epub", "Text/race.xhtml", "<p>source</p>", 0, 1, "", "")
        manager.set_pending_task_chains([[payload]])

        stale_task = manager.get_next_task("worker-old")
        manager.task_requeued("worker-old", stale_task)
        current_task = manager.get_next_task("worker-new")

        self.assertEqual(stale_task[0], current_task[0])
        self.assertFalse(
            manager.task_done_with_content("worker-old", stale_task, "stale translation", "gemini")
        )
        with manager._light_read_conn() as conn:
            task_row = conn.execute(
                "SELECT status, worker_id FROM tasks WHERE task_id = ?",
                (str(current_task[0]),),
            ).fetchone()
            result_count = conn.execute(
                "SELECT COUNT(*) FROM chunk_results WHERE task_id = ?",
                (str(current_task[0]),),
            ).fetchone()[0]

        self.assertEqual(task_row["status"], "in_progress")
        self.assertEqual(task_row["worker_id"], "worker-new")
        self.assertEqual(result_count, 0)

        self.assertTrue(
            manager.task_done_with_content("worker-new", current_task, "current translation", "gemini")
        )
        with manager._light_read_conn() as conn:
            saved_result = conn.execute(
                "SELECT translated_content FROM chunk_results WHERE task_id = ?",
                (str(current_task[0]),),
            ).fetchone()[0]
        self.assertEqual(decompress_chunk_content(saved_result), "current translation")


if __name__ == "__main__":
    unittest.main()
