"""Portable, path-safe backups for translatorFork project folders."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from .archive_safety import validate_zip_for_extraction


BACKUP_FORMAT = "translatorFork-project-backup"
BACKUP_VERSION = 1
MAX_MANIFEST_BYTES = 1024 * 1024
_EXCLUDED_DIRS = {".git", ".venv", ".venv-linux", "__pycache__", "node_modules"}
_EXCLUDED_SUFFIXES = (".tmp", ".lock")


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _safe_relative(value: str) -> Path:
    normalized = str(value or "").replace("\\", "/")
    parts = normalized.split("/")
    if (
        not normalized
        or normalized.startswith("/")
        or (len(normalized) > 1 and normalized[1] == ":")
        or any(part in {"", ".", ".."} for part in parts)
        or any(any(char in '<>:"|?*' or ord(char) < 32 for char in part) for part in parts)
        or any(part.endswith((".", " ")) for part in parts)
        or any(re.match(r"(?i)^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", part) for part in parts)
    ):
        raise ValueError(f"Некорректный путь в резервной копии: {value!r}")
    return Path(*parts)


def _write_file_to_archive(archive: zipfile.ZipFile, path: Path, archive_name: str, scratch_dir: Path) -> None:
    if path.suffix.lower() not in {".db", ".sqlite", ".sqlite3"}:
        archive.write(path, archive_name)
        return
    try:
        with path.open("rb") as handle:
            is_sqlite = handle.read(16) == b"SQLite format 3\x00"
    except OSError:
        is_sqlite = False
    if not is_sqlite:
        archive.write(path, archive_name)
        return

    snapshot = scratch_dir / f"snapshot-{len(archive.filelist)}.sqlite"
    source_connection = None
    target_connection = None
    try:
        source_connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5)
        target_connection = sqlite3.connect(snapshot)
        source_connection.backup(target_connection)
        target_connection.close()
        target_connection = None
        archive.write(snapshot, archive_name)
    except sqlite3.Error as exc:
        raise RuntimeError(f"Не удалось сделать согласованный снимок базы {path.name}: {exc}") from exc
    finally:
        if source_connection is not None:
            source_connection.close()
        if target_connection is not None:
            target_connection.close()
        snapshot.unlink(missing_ok=True)


def create_project_backup(project_dir: str | os.PathLike, source_file: str | os.PathLike | None, backup_path: str | os.PathLike) -> Path:
    root = Path(project_dir).expanduser().resolve(strict=True)
    destination = Path(backup_path).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("Папка проекта не найдена")
    if _is_within(destination, root):
        raise ValueError("Файл резервной копии нельзя сохранять внутри папки проекта")

    source = Path(source_file).expanduser().resolve(strict=True) if source_file else None
    source_rel = None
    if source:
        source_rel = f"project/{source.relative_to(root).as_posix()}" if _is_within(source, root) else f"__source__/{source.name}"

    manifest = {
        "format": BACKUP_FORMAT,
        "version": BACKUP_VERSION,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_file": source_rel,
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}-", suffix=".tmp", dir=destination.parent)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        with tempfile.TemporaryDirectory(prefix="translatorfork-backup-") as scratch:
            scratch_path = Path(scratch)
            with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                archive.writestr("backup_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
                for current_root, dirnames, filenames in os.walk(root, followlinks=False):
                    current = Path(current_root)
                    dirnames[:] = [
                        name for name in dirnames
                        if name not in _EXCLUDED_DIRS and not (current / name).is_symlink()
                    ]
                    for filename in filenames:
                        path = current / filename
                        if path.is_symlink() or filename.endswith(_EXCLUDED_SUFFIXES):
                            continue
                        if path.resolve() == destination or path.resolve() == temp_path.resolve():
                            continue
                        relative = path.relative_to(root).as_posix()
                        _write_file_to_archive(archive, path, f"project/{relative}", scratch_path)
                if source and not _is_within(source, root):
                    _write_file_to_archive(archive, source, source_rel, scratch_path)
        with zipfile.ZipFile(temp_path, "r") as check_archive:
            validate_zip_for_extraction(check_archive)
        os.replace(temp_path, destination)
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return destination


def restore_project_backup(backup_path: str | os.PathLike, destination_parent: str | os.PathLike) -> tuple[Path, Path | None]:
    backup = Path(backup_path).expanduser().resolve(strict=True)
    parent = Path(destination_parent).expanduser().resolve(strict=True)
    if not backup.is_file() or not parent.is_dir():
        raise ValueError("Не найден архив резервной копии или папка восстановления")

    with zipfile.ZipFile(backup, "r") as archive:
        validate_zip_for_extraction(archive)
        try:
            manifest_info = archive.getinfo("backup_manifest.json")
        except KeyError as exc:
            raise ValueError("В архиве нет манифеста резервной копии") from exc
        if manifest_info.file_size > MAX_MANIFEST_BYTES:
            raise ValueError("Манифест резервной копии слишком большой")
        manifest = json.loads(archive.read(manifest_info).decode("utf-8"))
        if manifest.get("format") != BACKUP_FORMAT or manifest.get("version") != BACKUP_VERSION:
            raise ValueError("Формат резервной копии не поддерживается")

        source_value = manifest.get("source_file")
        source_rel = _safe_relative(source_value) if source_value else None
        allowed_source = source_rel.as_posix() if source_rel else None
        files = []
        seen_paths = set()
        for info in archive.infolist():
            if info.filename == "backup_manifest.json" or info.is_dir():
                continue
            relative = _safe_relative(info.filename)
            if relative.parts[0] == "project":
                if len(relative.parts) < 2:
                    raise ValueError("В резервной копии обнаружен некорректный путь проекта")
                output_rel = relative
            elif allowed_source == relative.as_posix() and relative.parts[0] == "__source__":
                output_rel = relative
            else:
                raise ValueError(f"Неожиданный файл в резервной копии: {info.filename!r}")
            normalized_output = output_rel.as_posix().casefold()
            if normalized_output in seen_paths:
                raise ValueError(f"В резервной копии повторяется путь: {info.filename!r}")
            seen_paths.add(normalized_output)
            files.append((info, output_rel))

        folder_name = re.sub(r"[^A-Za-z0-9А-Яа-яЁё._ -]+", "_", backup.stem).strip(" .") or "restored_project"
        destination = parent / folder_name
        if destination.exists():
            raise FileExistsError(f"Папка уже существует: {destination}")
        staging = Path(tempfile.mkdtemp(prefix=f".{folder_name}-restore-", dir=parent))
        try:
            (staging / "project").mkdir()
            for info, relative in files:
                output = staging / relative
                output.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info, "r") as src, output.open("wb") as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
            restored_source = staging / source_rel if source_rel else None
            if restored_source is not None and not restored_source.is_file():
                raise ValueError("Исходный EPUB отсутствует в резервной копии")
            os.replace(staging, destination)
            source_path = destination / source_rel if source_rel else None
            return destination, source_path
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
