import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtWidgets

import os_patch
from gemini_translator.ui.dialogs import epub as epub_dialog
from gemini_translator.utils.memory_fs import MemoryFS


def test_isolated_memfs_copy_does_not_share_queue_resource(tmp_path, monkeypatch):
    source = tmp_path / "book.epub"
    source.write_bytes(b"epub-data")
    mem_fs = MemoryFS()
    monkeypatch.setattr(os_patch, "_get_or_create_mem_fs", lambda: mem_fs)

    try:
        queue_path = os_patch.copy_to_mem(str(source))
        dialog_path = os_patch.copy_to_mem(str(source), unique=True)

        assert queue_path != dialog_path
        assert mem_fs.exists(queue_path.removeprefix("mem://"))
        assert mem_fs.exists(dialog_path.removeprefix("mem://"))

        mem_fs.remove(dialog_path.removeprefix("mem://"))

        assert mem_fs.exists(queue_path.removeprefix("mem://"))
        with mem_fs.openbin(queue_path.removeprefix("mem://")) as queue_file:
            assert queue_file.read() == b"epub-data"
    finally:
        mem_fs.close()


def test_epub_selector_requests_its_own_memfs_copy(monkeypatch, tmp_path):
    _ = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    source = str(tmp_path / "book.epub")
    calls = []

    class SelectorHarness:
        virtual_epub_path = None
        real_epub_path = source
        all_chapters = []

        def _load_chapter_title_cache(self):
            pass

        def _populate_list_widget_preview(self):
            pass

        def _async_stage_3_load_details(self):
            pass

        def reject(self):
            raise AssertionError("selector should not reject a valid isolated copy")

    def fake_copy_to_mem(path, **kwargs):
        calls.append((path, kwargs))
        return "mem://isolated/dialog/book.epub"

    monkeypatch.setattr(epub_dialog.os, "copy_to_mem", fake_copy_to_mem, raising=False)
    monkeypatch.setattr(epub_dialog, "get_epub_chapter_order", lambda _path: [])
    monkeypatch.setattr(epub_dialog.QtCore.QTimer, "singleShot", lambda *_args: None)

    harness = SelectorHarness()
    epub_dialog.EpubHtmlSelectorDialog._async_stage_2_get_filelist(harness)

    assert calls == [(source, {"unique": True})]
