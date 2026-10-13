import os
import sqlite3
import uuid

from gemini_translator.core import task_manager as task_manager_module
from gemini_translator.core.task_manager import ChapterQueueManager


class _EventBus:
    def subscribe(self, *_args, **_kwargs):
        pass


def _make_manager():
    db_uri = f"file:snapshot-test-{uuid.uuid4().hex}?mode=memory&cache=shared"
    main_connection = sqlite3.connect(db_uri, uri=True, check_same_thread=False)
    manager = ChapterQueueManager(
        event_bus=_EventBus(),
        db_uri=db_uri,
        main_connection=main_connection,
    )
    return manager, main_connection


def test_save_queue_snapshot_preserves_existing_file_if_replace_fails(tmp_path, monkeypatch, qtbot):
    manager, main_connection = _make_manager()
    manager._log = print
    epub_path = tmp_path / "book.epub"
    epub_path.write_bytes(b"PK\x03\x04test epub data")
    snapshot_path = tmp_path / "queue.db"

    try:
        with main_connection:
            main_connection.execute(
                "INSERT INTO tasks (task_id, payload, status, sequence) VALUES (?, ?, ?, ?)",
                ("task-1", '["raw_text_translation", "chapter-1", "text"]', "pending", 1),
            )

        assert manager.save_queue_snapshot(str(snapshot_path), str(epub_path))
        previous_snapshot = snapshot_path.read_bytes()

        real_replace = task_manager_module.os.replace

        def fail_replace(source, destination):
            if os.fspath(destination) == os.fspath(snapshot_path):
                raise OSError("simulated interrupted snapshot publication")
            return real_replace(source, destination)

        monkeypatch.setattr(task_manager_module.os, "replace", fail_replace)
        assert not manager.save_queue_snapshot(str(snapshot_path), str(epub_path), quiet=True)
        assert snapshot_path.read_bytes() == previous_snapshot

        with sqlite3.connect(snapshot_path) as saved_conn:
            assert saved_conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert saved_conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    finally:
        main_connection.close()


def test_save_queue_snapshot_writes_complete_sqlite_file(tmp_path, qtbot):
    manager, main_connection = _make_manager()
    manager._log = print
    epub_path = tmp_path / "book.epub"
    epub_path.write_bytes(b"PK\x03\x04test epub data")
    snapshot_path = tmp_path / "queue.db"

    try:
        with main_connection:
            main_connection.execute(
                "INSERT INTO tasks (task_id, payload, status, sequence) VALUES (?, ?, ?, ?)",
                ("task-1", '["raw_text_translation", "chapter-1", "text"]', "pending", 1),
            )

        assert manager.save_queue_snapshot(str(snapshot_path), str(epub_path))
        with sqlite3.connect(snapshot_path) as saved_conn:
            assert saved_conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert saved_conn.execute("SELECT status FROM tasks WHERE task_id='task-1'").fetchone()[0] == "pending"
            assert saved_conn.execute("SELECT value FROM meta_info WHERE key='epub_sig'").fetchone()[0]
        assert list(tmp_path.glob(".queue.db.*.tmp")) == []
    finally:
        main_connection.close()
