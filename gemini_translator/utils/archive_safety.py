"""Resource and path checks for ZIP files supplied by users."""

from __future__ import annotations

import stat
import zipfile


MAX_ARCHIVE_ENTRIES = 20_000
MAX_ARCHIVE_MEMBER_SIZE = 512 * 1024 * 1024
MAX_ARCHIVE_TOTAL_SIZE = 2 * 1024 * 1024 * 1024
MAX_MEMBER_COMPRESSION_RATIO = 1_000


def validate_zip_for_extraction(archive: zipfile.ZipFile) -> None:
    """Reject ZIPs whose paths or declared expansion could escape or exhaust disk."""
    entries = archive.infolist()
    if len(entries) > MAX_ARCHIVE_ENTRIES:
        raise ValueError(f"В архиве слишком много файлов (максимум {MAX_ARCHIVE_ENTRIES}).")

    total_size = 0
    for entry in entries:
        name = entry.filename.replace("\\", "/")
        parts = name.split("/")
        if (
            name.startswith("/")
            or (len(name) >= 2 and name[1] == ":")
            or any(part == ".." for part in parts)
            or "\x00" in name
        ):
            raise ValueError(f"В архиве обнаружен небезопасный путь: {entry.filename!r}")
        unix_mode = (entry.external_attr >> 16) & 0xFFFF
        if stat.S_ISLNK(unix_mode):
            raise ValueError(f"Символические ссылки в архиве не поддерживаются: {entry.filename!r}")
        if entry.file_size > MAX_ARCHIVE_MEMBER_SIZE:
            raise ValueError(f"Файл в архиве слишком большой: {entry.filename!r}")
        total_size += entry.file_size
        if total_size > MAX_ARCHIVE_TOTAL_SIZE:
            raise ValueError("Распакованный архив превышает допустимый размер 2 ГБ.")
        if entry.file_size >= 1024 * 1024:
            ratio = entry.file_size / max(entry.compress_size, 1)
            if ratio > MAX_MEMBER_COMPRESSION_RATIO:
                raise ValueError(f"Подозрительная степень сжатия файла: {entry.filename!r}")
