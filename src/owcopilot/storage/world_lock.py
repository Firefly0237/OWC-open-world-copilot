"""Cross-thread and cross-process serialization for file-backed world mutations.

Atomic file replacement prevents torn JSON, but a read-modify-write sequence can still lose an
update when two API worker threads (or processes) load the same revision.  This advisory lock wraps
those complete sequences.  The lock file is retained; the operating system releases the lock if a
worker exits unexpectedly.
"""

from __future__ import annotations

import errno
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from importlib import import_module
from pathlib import Path
from typing import Any, Protocol, TypeVar, cast

_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_STATE = threading.local()
_WINDOWS_LOCK_RETRY_SECONDS = 0.05
_WINDOWS_LOCK_BUSY_ERRNOS = {errno.EACCES, errno.EAGAIN, errno.EDEADLK}
ReturnT = TypeVar("ReturnT")


class _WindowsLockingAPI(Protocol):
    """Typed boundary for the Windows-only subset of :mod:`msvcrt` used here."""

    LK_NBLCK: int
    LK_UNLCK: int

    def locking(self, descriptor: int, mode: int, byte_count: int, /) -> None: ...


def _windows_locking_api() -> _WindowsLockingAPI:
    """Load ``msvcrt`` lazily so non-Windows type checking and imports remain valid."""

    return cast(_WindowsLockingAPI, import_module("msvcrt"))


def _held_lock_keys() -> set[str]:
    held = getattr(_THREAD_STATE, "held_lock_keys", None)
    if held is None:
        held = set()
        _THREAD_STATE.held_lock_keys = held
    return held


def _try_windows_lock(descriptor: int) -> None:
    """Attempt the one-byte Windows lock once without msvcrt's fixed retry budget."""

    windows_lock = _windows_locking_api()
    os.lseek(descriptor, 0, os.SEEK_SET)
    windows_lock.locking(descriptor, windows_lock.LK_NBLCK, 1)


def _release_windows_lock(descriptor: int) -> None:
    """Release the one-byte Windows lock through the typed platform boundary."""

    windows_lock = _windows_locking_api()
    os.lseek(descriptor, 0, os.SEEK_SET)
    windows_lock.locking(descriptor, windows_lock.LK_UNLCK, 1)


def _acquire_windows_lock(descriptor: int) -> None:
    """Wait for a Windows byte-range lock until it becomes available.

    ``msvcrt.LK_LOCK`` retries only a small fixed number of times before raising.  That turns a
    healthy long-running writer into an unrelated request's 500 response.  Polling ``LK_NBLCK``
    gives Windows the same blocking semantics as POSIX ``flock`` while still propagating genuine
    filesystem errors.
    """

    while True:
        try:
            _try_windows_lock(descriptor)
            return
        except OSError as exc:
            if exc.errno not in _WINDOWS_LOCK_BUSY_ERRNOS:
                raise
            time.sleep(_WINDOWS_LOCK_RETRY_SECONDS)


def _acquire_os_lock(descriptor: int) -> None:
    if os.name == "nt":
        _acquire_windows_lock(descriptor)
        return
    posix_lock: Any = __import__("fcntl")
    posix_lock.flock(descriptor, posix_lock.LOCK_EX)


def _release_os_lock(descriptor: int) -> None:
    if os.name == "nt":
        _release_windows_lock(descriptor)
        return
    os.lseek(descriptor, 0, os.SEEK_SET)
    posix_lock: Any = __import__("fcntl")
    posix_lock.flock(descriptor, posix_lock.LOCK_UN)


@contextmanager
def world_write_lock(content_root: str | Path) -> Iterator[None]:
    """Hold one exclusive lock for a world's complete read-modify-write operation."""

    root = Path(content_root).resolve(strict=False)
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"content root does not exist: {root}")
    lock_path = root / ".owcopilot" / "world-write.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    key = os.path.normcase(str(lock_path))
    with _LOCKS_GUARD:
        thread_lock = _THREAD_LOCKS.setdefault(key, threading.RLock())

    with thread_lock:
        held_lock_keys = _held_lock_keys()
        if key in held_lock_keys:
            # The outer context already owns both the per-process RLock and the OS lock. Opening a
            # second descriptor and locking the same byte deadlocks on both Windows and POSIX.
            yield
            return

        descriptor = os.open(
            lock_path,
            os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0),
            0o600,
        )
        os_locked = False
        try:
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            _acquire_os_lock(descriptor)
            os_locked = True
            held_lock_keys.add(key)
            yield
        finally:
            held_lock_keys.discard(key)
            try:
                if os_locked:
                    _release_os_lock(descriptor)
            finally:
                os.close(descriptor)


def serialized_world_write(function: Callable[..., ReturnT]) -> Callable[..., ReturnT]:
    """Decorate an action whose first argument is its file-backed content root."""

    @wraps(function)
    def wrapped(content_root: str | Path, *args: Any, **kwargs: Any) -> ReturnT:
        with world_write_lock(content_root):
            return function(content_root, *args, **kwargs)

    return wrapped


__all__ = ["serialized_world_write", "world_write_lock"]
