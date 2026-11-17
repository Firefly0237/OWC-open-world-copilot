from __future__ import annotations

import pytest

from owcopilot.trust import PathSecurityError, resolve_under_root


def test_resolve_under_root_accepts_relative_child(tmp_path) -> None:
    resolved = resolve_under_root(tmp_path, "exports/manifest.json")

    assert resolved == (tmp_path / "exports" / "manifest.json").resolve()


def test_resolve_under_root_rejects_parent_escape(tmp_path) -> None:
    with pytest.raises(PathSecurityError, match="escapes allowed root"):
        resolve_under_root(tmp_path, "../outside.json")


def test_resolve_under_root_accepts_absolute_child(tmp_path) -> None:
    child = tmp_path / "content" / "file.json"

    assert resolve_under_root(tmp_path, child) == child.resolve()
