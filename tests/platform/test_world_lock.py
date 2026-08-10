"""Cross-platform regression tests for the file-backed world write lock."""

from __future__ import annotations

import errno
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

import owcopilot.storage.world_lock as world_lock_module


def _run_python(code: str, *args: object, timeout: float = 8.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code), *(str(arg) for arg in args)],
        capture_output=True,
        check=False,
        text=True,
        timeout=timeout,
    )


def test_world_write_lock_is_reentrant_in_one_thread(tmp_path: Path) -> None:
    root = tmp_path / "content"
    root.mkdir()

    # Exercise a real OS lock in a subprocess so a regression fails on timeout instead of hanging
    # the pytest worker indefinitely (POSIX flock and Windows byte locks both self-conflict when a
    # second independently-opened descriptor is used).
    result = _run_python(
        """
        import sys
        from owcopilot.storage.world_lock import world_write_lock

        with world_write_lock(sys.argv[1]):
            with world_write_lock(sys.argv[1]):
                print("nested")
        """,
        root,
        timeout=4.0,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "nested"


def test_windows_retry_loop_has_no_fixed_attempt_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0
    waits: list[float] = []

    def contend_then_acquire(_descriptor: int) -> None:
        nonlocal attempts
        attempts += 1
        if attempts <= 25:
            raise OSError(errno.EDEADLK, "lock is busy")

    monkeypatch.setattr(world_lock_module, "_try_windows_lock", contend_then_acquire)
    monkeypatch.setattr(world_lock_module.time, "sleep", waits.append)

    world_lock_module._acquire_windows_lock(123)

    assert attempts == 26
    assert waits == [world_lock_module._WINDOWS_LOCK_RETRY_SECONDS] * 25


@pytest.mark.skipif(os.name != "nt", reason="exercises the Windows msvcrt contention path")
def test_windows_world_lock_waits_beyond_msvcrt_legacy_timeout(tmp_path: Path) -> None:
    root = tmp_path / "content"
    root.mkdir()
    holder_code = """
        import sys
        import time
        from owcopilot.storage.world_lock import world_write_lock

        with world_write_lock(sys.argv[1]):
            print("held", flush=True)
            time.sleep(12.5)
    """
    waiter_code = """
        import sys
        from owcopilot.storage.world_lock import world_write_lock

        with world_write_lock(sys.argv[1]):
            print("acquired")
    """
    holder = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(holder_code), str(root)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"
        started = time.monotonic()
        waiter = _run_python(waiter_code, root, timeout=20.0)
        elapsed = time.monotonic() - started

        assert waiter.returncode == 0, waiter.stderr
        assert waiter.stdout.strip() == "acquired"
        assert elapsed >= 9.0
    finally:
        if holder.poll() is None:
            holder.terminate()
            try:
                holder.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                holder.kill()
                holder.wait(timeout=3.0)
