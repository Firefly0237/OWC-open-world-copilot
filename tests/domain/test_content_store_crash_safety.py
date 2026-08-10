"""Crash/fault matrix for the shared single-file atomic writer."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from owcopilot.storage import atomic_file

_CHILD = r"""
import os
import sys
from pathlib import Path

from owcopilot.storage import atomic_file

target = Path(sys.argv[1])
phase = sys.argv[2]
real_replace = atomic_file.os.replace

def crash_replace(source, destination):
    if phase == "before":
        os._exit(71)
    real_replace(source, destination)
    os._exit(72)

atomic_file.os.replace = crash_replace
atomic_file.atomic_write_text(target, "new-complete\n")
"""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize(
    ("phase", "return_code", "expected"),
    [("before", 71, b"old-complete\n"), ("after", 72, b"new-complete\n")],
)
def test_os_exit_before_or_after_replace_leaves_only_a_complete_version(
    tmp_path: Path, phase: str, return_code: int, expected: bytes
) -> None:
    target = tmp_path / "ledger.json"
    old = b"old-complete\n"
    new = b"new-complete\n"
    target.write_bytes(old)
    env = os.environ.copy()
    source_root = Path(__file__).resolve().parents[2] / "src"
    env["PYTHONPATH"] = os.pathsep.join(
        [str(source_root), value] if (value := env.get("PYTHONPATH")) else [str(source_root)]
    )

    completed = subprocess.run(
        [sys.executable, "-c", _CHILD, str(target), phase],
        check=False,
        cwd=Path(__file__).resolve().parents[2],
        env=env,
    )

    assert completed.returncode == return_code
    actual = target.read_bytes()
    assert _sha(actual) in {_sha(old), _sha(new)}
    assert actual == expected


def test_replace_exception_cleans_unique_same_directory_temp_and_keeps_old(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state.json"
    target.write_text("old", encoding="utf-8")
    seen_temp: list[Path] = []

    def fail_replace(source: str | Path, destination: str | Path) -> None:
        temp = Path(source)
        seen_temp.append(temp)
        assert temp.parent == target.parent
        assert temp != target
        assert Path(destination) == target
        assert temp.read_text(encoding="utf-8") == "new"
        raise OSError("injected replace failure")

    monkeypatch.setattr(atomic_file.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected replace failure"):
        atomic_file.atomic_write_text(target, "new")

    assert target.read_text(encoding="utf-8") == "old"
    assert seen_temp and not seen_temp[0].exists()
    assert not list(tmp_path.glob(".state.json.*.tmp"))


def test_replace_happens_after_file_fsync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "ordered.json"
    events: list[str] = []
    real_fsync = atomic_file.os.fsync
    real_replace = atomic_file.os.replace

    def recording_fsync(fd: int) -> None:
        events.append("fsync")
        real_fsync(fd)

    def recording_replace(source: str | Path, destination: str | Path) -> None:
        events.append("replace")
        real_replace(source, destination)

    monkeypatch.setattr(atomic_file.os, "fsync", recording_fsync)
    monkeypatch.setattr(atomic_file.os, "replace", recording_replace)
    atomic_file.atomic_write_text(target, "complete")

    assert target.read_text(encoding="utf-8") == "complete"
    assert events[0] == "fsync"
    assert events.index("fsync") < events.index("replace")
