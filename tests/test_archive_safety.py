import io
import stat
import zipfile

import pytest

from gemini_translator.utils import archive_safety


def _archive_with_entry(name, data=b"payload", *, external_attr=0):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        entry = zipfile.ZipInfo(name)
        entry.external_attr = external_attr
        archive.writestr(entry, data)
    buffer.seek(0)
    return zipfile.ZipFile(buffer, "r")


@pytest.mark.parametrize("name", ["../outside.txt", r"..\outside.txt", "/absolute.txt", "C:/outside.txt"])
def test_rejects_archive_paths_that_escape_extraction_root(name):
    archive = _archive_with_entry(name)
    try:
        with pytest.raises(ValueError, match="небезопасный путь"):
            archive_safety.validate_zip_for_extraction(archive)
    finally:
        archive.close()


def test_rejects_symlink_entries():
    symlink_mode = (stat.S_IFLNK | 0o777) << 16
    archive = _archive_with_entry("chapter.xhtml", b"target", external_attr=symlink_mode)
    try:
        with pytest.raises(ValueError, match="Символические ссылки"):
            archive_safety.validate_zip_for_extraction(archive)
    finally:
        archive.close()


def test_rejects_archive_with_too_many_entries(monkeypatch):
    monkeypatch.setattr(archive_safety, "MAX_ARCHIVE_ENTRIES", 1)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as writer:
        writer.writestr("one", b"1")
        writer.writestr("two", b"2")
    archive.seek(0)

    with zipfile.ZipFile(archive, "r") as reader:
        with pytest.raises(ValueError, match="слишком много файлов"):
            archive_safety.validate_zip_for_extraction(reader)


def test_rejects_excessive_declared_uncompressed_size(monkeypatch):
    monkeypatch.setattr(archive_safety, "MAX_ARCHIVE_MEMBER_SIZE", 4)
    archive = _archive_with_entry("large.txt", b"12345")
    try:
        with pytest.raises(ValueError, match="слишком большой"):
            archive_safety.validate_zip_for_extraction(archive)
    finally:
        archive.close()


def test_rejects_excessive_total_uncompressed_size(monkeypatch):
    monkeypatch.setattr(archive_safety, "MAX_ARCHIVE_TOTAL_SIZE", 8)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as writer:
        writer.writestr("one.txt", b"12345")
        writer.writestr("two.txt", b"67890")
    buffer.seek(0)

    with zipfile.ZipFile(buffer, "r") as archive:
        with pytest.raises(ValueError, match="превышает допустимый размер"):
            archive_safety.validate_zip_for_extraction(archive)


def test_rejects_suspicious_compression_ratio(monkeypatch):
    monkeypatch.setattr(archive_safety, "MAX_MEMBER_COMPRESSION_RATIO", 2)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as writer:
        writer.writestr("highly-compressible.txt", b"a" * (1024 * 1024))
    buffer.seek(0)

    with zipfile.ZipFile(buffer, "r") as archive:
        with pytest.raises(ValueError, match="степень сжатия"):
            archive_safety.validate_zip_for_extraction(archive)
