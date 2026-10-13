import errno
import os

import os_patch
from gemini_translator.utils.memory_fs import MemoryFS


def test_os_patch_routes_virtual_file_operations_to_memory_fs(monkeypatch):
    mem_fs = MemoryFS()
    monkeypatch.setattr(os_patch, "_get_or_create_mem_fs", lambda: mem_fs)
    virtual_dir = "mem://session/chapter"
    virtual_file = os_patch.HybridPath().join(virtual_dir, "translation.txt")

    try:
        assert virtual_file == "mem://session/chapter/translation.txt"
        os_patch._patched_makedirs(virtual_dir, exist_ok=True)
        with os_patch._patched_open(virtual_file, "w", encoding="utf-8") as output:
            output.write("Переведённый текст")

        assert os_patch._patched_exists(virtual_file)
        assert os_patch._patched_isfile(virtual_file)
        assert os_patch._patched_isdir(virtual_dir)
        assert os_patch._patched_listdir(virtual_dir) == ["translation.txt"]
        with os_patch._patched_open(virtual_file, "r", encoding="utf-8") as source:
            assert source.read() == "Переведённый текст"

        renamed_file = os_patch.HybridPath().join(virtual_dir, "renamed.txt")
        os_patch._patched_rename(virtual_file, renamed_file)
        assert not os_patch._patched_exists(virtual_file)
        assert os_patch._patched_exists(renamed_file)
        os_patch._patched_remove(renamed_file)
        assert not os_patch._patched_exists(renamed_file)
    finally:
        mem_fs.close()


def test_large_files_spill_to_disk_and_round_trip_without_using_memory_budget(tmp_path):
    mem_fs = MemoryFS(max_memory_bytes=32, spill_threshold_bytes=16, max_total_bytes=1024, temp_dir=str(tmp_path))
    payload = b"large epub payload" * 20
    spill_dir = mem_fs._temp_dir.name

    try:
        mem_fs.makedirs("/books")
        with mem_fs.openbin("/books/book.epub", "wb") as output:
            for offset in range(0, len(payload), 13):
                output.write(payload[offset:offset + 13])

        assert mem_fs.memory_bytes <= 32
        assert mem_fs.total_bytes == len(payload)
        assert mem_fs.spilled_file_count == 1
        with mem_fs.openbin("/books/book.epub") as source:
            assert source.read() == payload
        mem_fs.move("/books/book.epub", "/books/renamed.epub")
        assert mem_fs.exists("/books/renamed.epub")
        mem_fs.remove("/books/renamed.epub")
        assert mem_fs.total_bytes == 0
        assert mem_fs.spilled_file_count == 0
    finally:
        mem_fs.close()

    assert not os.path.exists(spill_dir)


def test_memory_budget_spills_small_files_after_budget_is_full(tmp_path):
    mem_fs = MemoryFS(max_memory_bytes=10, spill_threshold_bytes=64, max_total_bytes=1024, temp_dir=str(tmp_path))
    try:
        mem_fs.writebytes("/first.bin", b"123456")
        mem_fs.writebytes("/second.bin", b"abcdef")

        assert mem_fs.memory_bytes == 6
        assert mem_fs.spilled_file_count == 1
        assert mem_fs.total_bytes == 12
    finally:
        mem_fs.close()


def test_memory_fs_rejects_files_beyond_total_storage_limit(tmp_path):
    mem_fs = MemoryFS(max_memory_bytes=4, spill_threshold_bytes=4, max_total_bytes=8, temp_dir=str(tmp_path))
    try:
        mem_fs.writebytes("/first.bin", b"12345")
        try:
            mem_fs.writebytes("/second.bin", b"6789")
        except OSError as exc:
            assert exc.errno == errno.ENOSPC
        else:
            raise AssertionError("Expected the configured virtual storage limit to reject the write")
        assert mem_fs.total_bytes == 5
    finally:
        mem_fs.close()


def test_copy_to_mem_streams_large_source_into_disk_backing(tmp_path, monkeypatch):
    source = tmp_path / "large.epub"
    payload = b"epub-data-" * 100
    source.write_bytes(payload)
    mem_fs = MemoryFS(max_memory_bytes=16, spill_threshold_bytes=16, max_total_bytes=4096, temp_dir=str(tmp_path))
    monkeypatch.setattr(os_patch, "_get_or_create_mem_fs", lambda: mem_fs)

    try:
        virtual_path = os_patch.copy_to_mem(str(source))

        assert virtual_path is not None
        assert mem_fs.memory_bytes == 0
        assert mem_fs.spilled_file_count == 1
        with mem_fs.openbin(virtual_path.removeprefix("mem://")) as copied:
            assert copied.read() == payload
    finally:
        mem_fs.close()
