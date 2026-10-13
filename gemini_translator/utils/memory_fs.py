"""Bounded, process-local filesystem for the app's ``mem://`` paths.

Small files stay in memory. Large files, or writes which exceed the shared
memory budget, are transparently stored in a private temporary directory.
"""

from __future__ import annotations

import errno
import io
import os
import posixpath
import shutil
import tempfile
import threading
import types
import uuid
from collections.abc import Callable


DEFAULT_MEMORY_BUDGET_BYTES = 64 * 1024 * 1024
DEFAULT_SPILL_THRESHOLD_BYTES = 8 * 1024 * 1024
DEFAULT_TOTAL_LIMIT_BYTES = 8 * 1024 * 1024 * 1024


class _MemoryWriter(io.BufferedIOBase):
    def __init__(self, fs: "MemoryFS", path: str, initial, mode: str):
        super().__init__()
        self._fs = fs
        self._path = path
        self._mode = mode
        self._stream = tempfile.SpooledTemporaryFile(
            max_size=fs.spill_threshold_bytes,
            mode="w+b",
            dir=fs._temp_dir.name,
        )
        if isinstance(initial, bytes):
            self._stream.write(initial)
        elif isinstance(initial, str):
            with open(initial, "rb") as source:
                shutil.copyfileobj(source, self._stream, length=1024 * 1024)
        self._stream.seek(0, io.SEEK_END if "a" in mode else io.SEEK_SET)

    def readable(self) -> bool:
        return "+" in self._mode

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        if not self.readable():
            raise io.UnsupportedOperation("not readable")
        data = self._stream.read(len(buffer))
        buffer[:len(data)] = data
        return len(data)

    def write(self, data) -> int:
        if self.closed:
            raise ValueError("I/O operation on closed file")
        position = self._stream.tell()
        self._stream.seek(0, io.SEEK_END)
        current_size = self._stream.tell()
        self._stream.seek(position)
        requested_size = max(current_size, position + len(data))
        if requested_size > self._fs.max_total_bytes:
            raise OSError(errno.EFBIG, "Virtual file exceeds the configured size limit")
        return self._stream.write(data)

    def seek(self, offset, whence=io.SEEK_SET):
        return self._stream.seek(offset, whence)

    def tell(self):
        return self._stream.tell()

    def truncate(self, size=None):
        return self._stream.truncate(size)

    def flush(self) -> None:
        if not self._stream.closed:
            self._stream.flush()

    def close(self) -> None:
        if self.closed:
            return
        try:
            self._fs._save_stream(self._path, self._stream)
        finally:
            try:
                super().close()
            finally:
                self._stream.close()


class MemoryFS:
    """A narrow filesystem API matching the operations used by ``os_patch``."""

    def __init__(
        self,
        *,
        max_memory_bytes: int = DEFAULT_MEMORY_BUDGET_BYTES,
        spill_threshold_bytes: int = DEFAULT_SPILL_THRESHOLD_BYTES,
        max_total_bytes: int = DEFAULT_TOTAL_LIMIT_BYTES,
        temp_dir: str | None = None,
    ):
        if max_memory_bytes < 0 or spill_threshold_bytes < 0 or max_total_bytes <= 0:
            raise ValueError("MemoryFS limits must be non-negative and total limit must be positive")
        self.max_memory_bytes = int(max_memory_bytes)
        self.spill_threshold_bytes = int(spill_threshold_bytes)
        self.max_total_bytes = int(max_total_bytes)
        self._temp_dir = tempfile.TemporaryDirectory(prefix="translator-memfs-", dir=temp_dir)
        self._files: dict[str, bytes | str] = {}
        self._file_sizes: dict[str, int] = {}
        self._memory_bytes = 0
        self._total_bytes = 0
        self._dirs = {"/"}
        self._lock = threading.RLock()
        self._closed = False

    @staticmethod
    def _path(path: str) -> str:
        normalized = posixpath.normpath("/" + str(path).replace("\\", "/").lstrip("/"))
        return normalized if normalized.startswith("/") else "/" + normalized

    def _ensure_open(self) -> None:
        if self._closed:
            raise ValueError("I/O operation on closed memory filesystem")

    @property
    def memory_bytes(self) -> int:
        with self._lock:
            return self._memory_bytes

    @property
    def total_bytes(self) -> int:
        with self._lock:
            return self._total_bytes

    @property
    def spilled_file_count(self) -> int:
        with self._lock:
            return sum(isinstance(value, str) for value in self._files.values())

    def exists(self, path: str) -> bool:
        key = self._path(path)
        with self._lock:
            self._ensure_open()
            return key in self._files or key in self._dirs

    def isdir(self, path: str) -> bool:
        with self._lock:
            self._ensure_open()
            return self._path(path) in self._dirs

    def isfile(self, path: str) -> bool:
        with self._lock:
            self._ensure_open()
            return self._path(path) in self._files

    def listdir(self, path: str) -> list[str]:
        key = self._path(path)
        with self._lock:
            self._ensure_open()
            if key not in self._dirs:
                if key in self._files:
                    raise NotADirectoryError(path)
                raise FileNotFoundError(path)
            prefix = key.rstrip("/") + "/" if key != "/" else "/"
            children = set()
            for candidate in (*self._dirs, *self._files):
                if candidate.startswith(prefix) and candidate != key:
                    remainder = candidate[len(prefix):]
                    if remainder:
                        children.add(remainder.split("/", 1)[0])
            return sorted(children)

    def makedirs(self, path: str, recreate: bool = False, *, exist_ok: bool | None = None, **_kwargs) -> None:
        key = self._path(path)
        allow_existing = recreate if exist_ok is None else exist_ok
        with self._lock:
            self._ensure_open()
            if key in self._files:
                raise FileExistsError(path)
            if key in self._dirs and not allow_existing:
                raise FileExistsError(path)
            parts = key.strip("/").split("/") if key != "/" else []
            current = ""
            for part in parts:
                current += "/" + part
                if current in self._files:
                    raise FileExistsError(current)
                self._dirs.add(current)

    def _discard_file(self, key: str) -> None:
        previous = self._files.pop(key, None)
        previous_size = self._file_sizes.pop(key, 0)
        self._total_bytes -= previous_size
        if isinstance(previous, bytes):
            self._memory_bytes -= previous_size
        elif isinstance(previous, str):
            try:
                os.remove(previous)
            except FileNotFoundError:
                pass

    def remove(self, path: str) -> None:
        key = self._path(path)
        with self._lock:
            self._ensure_open()
            if key in self._dirs:
                raise IsADirectoryError(path)
            if key not in self._files:
                raise FileNotFoundError(path)
            self._discard_file(key)

    def move(self, src: str, dst: str) -> None:
        source, destination = self._path(src), self._path(dst)
        with self._lock:
            self._ensure_open()
            if destination in self._files or destination in self._dirs:
                raise FileExistsError(dst)
            parent = posixpath.dirname(destination)
            if parent not in self._dirs:
                raise FileNotFoundError(parent)
            if source in self._files:
                self._files[destination] = self._files.pop(source)
                self._file_sizes[destination] = self._file_sizes.pop(source)
                return
            if source not in self._dirs:
                raise FileNotFoundError(src)
            if destination.startswith(source.rstrip("/") + "/"):
                raise ValueError("Cannot move a directory inside itself")
            moved_dirs = {item: destination + item[len(source):] for item in self._dirs if item == source or item.startswith(source.rstrip("/") + "/")}
            moved_files = {item: destination + item[len(source):] for item in self._files if item.startswith(source.rstrip("/") + "/")}
            self._dirs.difference_update(moved_dirs)
            self._dirs.update(moved_dirs.values())
            for old, new in moved_files.items():
                self._files[new] = self._files.pop(old)
                self._file_sizes[new] = self._file_sizes.pop(old)

    def openbin(self, path: str, mode: str = "r"):
        key = self._path(path)
        writing = any(flag in mode for flag in "wax+")
        truncate = "w" in mode
        append = "a" in mode
        exclusive = "x" in mode
        with self._lock:
            self._ensure_open()
            if key in self._dirs:
                raise IsADirectoryError(path)
            exists = key in self._files
            if exclusive and exists:
                raise FileExistsError(path)
            if not exists and not any(flag in mode for flag in "wax"):
                raise FileNotFoundError(path)
            parent = posixpath.dirname(key)
            if parent not in self._dirs:
                raise FileNotFoundError(parent)
            initial = None if truncate or not exists else self._files[key]
            if writing:
                return _MemoryWriter(self, key, initial, mode)
            if isinstance(initial, str):
                return open(initial, "rb")
            return io.BytesIO(initial)

    def open(self, path: str, mode: str = "r", encoding: str = "utf-8", errors: str = "strict", newline=None):
        binary_mode = mode if "b" in mode else mode + "b"
        raw = self.openbin(path, binary_mode)
        if "b" in mode:
            return raw
        return io.TextIOWrapper(raw, encoding=encoding, errors=errors, newline=newline)

    def _save_stream(self, path: str, stream) -> None:
        stream.flush()
        stream.seek(0, io.SEEK_END)
        size = stream.tell()
        stream.seek(0)
        with self._lock:
            self._ensure_open()
            old_size = self._file_sizes.get(path, 0)
            total_size = self._total_bytes - old_size + size
            if total_size > self.max_total_bytes:
                raise OSError(errno.ENOSPC, "MemoryFS total storage limit exceeded")

            old_value = self._files.get(path)
            old_memory = old_size if isinstance(old_value, bytes) else 0
            keep_in_memory = (
                size <= self.spill_threshold_bytes
                and self._memory_bytes - old_memory + size <= self.max_memory_bytes
            )
            if keep_in_memory:
                new_value: bytes | str = stream.read()
            else:
                new_value = os.path.join(self._temp_dir.name, uuid.uuid4().hex)
                try:
                    with open(new_value, "wb") as disk_file:
                        shutil.copyfileobj(stream, disk_file, length=1024 * 1024)
                except Exception:
                    try:
                        os.remove(new_value)
                    except OSError:
                        pass
                    raise

            self._files[path] = new_value
            self._file_sizes[path] = size
            self._total_bytes = total_size
            self._memory_bytes = self._memory_bytes - old_memory + (size if isinstance(new_value, bytes) else 0)
            if isinstance(old_value, str):
                try:
                    os.remove(old_value)
                except FileNotFoundError:
                    pass

    def writebytes(self, path: str, data: bytes) -> None:
        key = self._path(path)
        with self._lock:
            self._ensure_open()
            if posixpath.dirname(key) not in self._dirs:
                raise FileNotFoundError(posixpath.dirname(key))
        with self.openbin(key, "wb") as stream:
            stream.write(data)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._files.clear()
            self._file_sizes.clear()
            self._memory_bytes = 0
            self._total_bytes = 0
            self._dirs = {"/"}
            self._closed = True
            self._temp_dir.cleanup()


path = types.SimpleNamespace(**{
    name: getattr(posixpath, name)
    for name in dir(posixpath)
    if not name.startswith("_")
})
