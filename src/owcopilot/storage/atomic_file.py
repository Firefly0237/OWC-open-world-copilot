"""Crash-safe primitives for mutable single-file state.

This module deliberately stops at one-file atomicity.  It does not provide revision pointers,
multi-file transactions, or a publication protocol; callers that update several files still own
the consistency contract between those files.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


class CorruptJSONError(ValueError):
    """Guided error for syntactically damaged on-disk JSON.

    Keeping the resolved path and exact decoder location on the exception makes HTTP/CLI error
    boundaries useful without ever pretending a damaged ledger is an empty one.
    """

    def __init__(self, path: str | Path, *, line: int, column: int, detail: str) -> None:
        self.path = Path(path).resolve()
        self.line = line
        self.column = column
        super().__init__(
            f"损坏的 JSON 文件 {self.path}（line {line}, column {column}）：{detail}。"
            "请修复该文件或从备份恢复；为避免数据丢失，未将其当作空数据。"
        )


def atomic_write_bytes(path: str | Path, payload: bytes) -> None:
    """Atomically replace ``path`` with the complete byte payload.

    The temporary file is unique and lives beside the destination, so ``os.replace`` stays on the
    same filesystem.  Bytes are flushed and fsynced before the closed file is renamed.  A normal
    exception removes the temporary file; an uncatchable process death may leave only that uniquely
    named temp, while the destination itself remains either the complete old or complete new file.
    """

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    temp_path = Path(temp_name)
    try:
        handle = os.fdopen(fd, "wb")
        fd = -1  # ownership transferred to ``handle``
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
        _fsync_directory(target.parent)
    except BaseException:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            # Preserve the original write failure.  The uniquely named temp is never a readable
            # state file and a later write cannot mistake it for the destination.
            pass
        raise


def atomic_write_text(path: str | Path, text: str, *, encoding: str = "utf-8") -> None:
    """Atomically replace ``path`` with the complete encoded ``text``."""

    atomic_write_bytes(path, text.encode(encoding))


def parse_json_text(text: str, *, path: str | Path, line_offset: int = 0) -> Any:
    """Decode JSON and attach the absolute source path plus real line/column on corruption."""

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise CorruptJSONError(
            path,
            line=exc.lineno + line_offset,
            column=exc.colno,
            detail=exc.msg,
        ) from exc


def read_json_file(path: str | Path) -> Any:
    """Read one UTF-8 JSON file without converting corruption into empty state."""

    source = Path(path)
    return parse_json_text(source.read_text(encoding="utf-8"), path=source)


def _fsync_directory(directory: Path) -> None:
    """Best-effort parent-directory fsync where the platform exposes that capability."""

    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        directory_fd = os.open(str(directory), flags)
    except (NotImplementedError, OSError):
        return
    try:
        try:
            os.fsync(directory_fd)
        except (NotImplementedError, OSError):
            pass
    finally:
        os.close(directory_fd)


__all__ = [
    "CorruptJSONError",
    "atomic_write_bytes",
    "atomic_write_text",
    "parse_json_text",
    "read_json_file",
]
