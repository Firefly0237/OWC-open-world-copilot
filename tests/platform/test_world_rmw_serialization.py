"""Regression coverage for world-level serialization of file-backed read/modify/write flows."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any

import pytest

import owcopilot.pipeline.project as project_module
import owcopilot.storage.world_lock as world_lock_module
from owcopilot.app.actions.assets import asset_attach_action, asset_detach_action
from owcopilot.app.actions.assist import dismiss_sweep_finding_action
from owcopilot.app.actions.engine_sync import (
    delete_mapping_template_action,
    save_mapping_template_action,
)
from owcopilot.app.actions.localization import loc_assign_action, loc_transition_action
from owcopilot.app.actions.references import add_reference_action, remove_reference_action
from owcopilot.content.models import ContentBundle
from owcopilot.content.store import ContentStore

FILE_RMW_ACTIONS = (
    asset_attach_action,
    asset_detach_action,
    loc_transition_action,
    loc_assign_action,
    dismiss_sweep_finding_action,
    save_mapping_template_action,
    delete_mapping_template_action,
    add_reference_action,
    remove_reference_action,
)


class _LockEntered(RuntimeError):
    pass


class _StopAtLock:
    def __enter__(self) -> None:
        raise _LockEntered

    def __exit__(self, *_args: object) -> bool:
        return False


@pytest.mark.parametrize("action", FILE_RMW_ACTIONS, ids=lambda action: action.__name__)
def test_file_rmw_action_enters_world_lock(
    action: Callable[..., dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered: list[Path] = []

    def stop_at_lock(content_root: str | Path) -> _StopAtLock:
        entered.append(Path(content_root))
        return _StopAtLock()

    monkeypatch.setattr(world_lock_module, "world_write_lock", stop_at_lock)

    with pytest.raises(_LockEntered):
        action(tmp_path)

    assert entered == [tmp_path]


def test_project_index_snapshots_are_built_under_world_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content_root = tmp_path / "content"
    ContentStore(content_root).save(ContentBundle())
    lock_depth = 0
    events: list[tuple[str, int]] = []

    @contextmanager
    def probe_lock(root: str | Path) -> Iterator[None]:
        nonlocal lock_depth
        assert Path(root) == content_root
        lock_depth += 1
        try:
            yield
        finally:
            lock_depth -= 1

    def watched(name: str, function: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(function)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            events.append((name, lock_depth))
            return function(*args, **kwargs)

        return wrapper

    monkeypatch.setattr(project_module, "world_write_lock", probe_lock)
    monkeypatch.setattr(
        project_module.ContentStore,
        "load_scoped",
        watched("load_scoped", project_module.ContentStore.load_scoped),
    )
    monkeypatch.setattr(
        project_module,
        "build_content_graph",
        watched("build_content_graph", project_module.build_content_graph),
    )
    monkeypatch.setattr(
        project_module.SQLiteStore,
        "replace_content_index",
        watched("replace_content_index", project_module.SQLiteStore.replace_content_index),
    )
    monkeypatch.setattr(
        project_module.SQLiteStore,
        "replace_graph_edges",
        watched("replace_graph_edges", project_module.SQLiteStore.replace_graph_edges),
    )
    monkeypatch.setattr(
        project_module.ReferenceStore,
        "sync_index",
        watched("sync_reference_index", project_module.ReferenceStore.sync_index),
    )
    monkeypatch.setattr(
        project_module.ContentStore,
        "read_head",
        watched("read_head", project_module.ContentStore.read_head),
    )

    project = project_module.ProjectContext.open(content_root)
    try:
        assert events == [
            ("load_scoped", 1),
            ("build_content_graph", 1),
            ("replace_content_index", 1),
            ("replace_graph_edges", 1),
            ("sync_reference_index", 1),
        ]

        events.clear()
        project.reload()
        assert events == [
            ("load_scoped", 1),
            ("build_content_graph", 1),
            ("replace_content_index", 1),
            ("replace_graph_edges", 1),
            ("sync_reference_index", 1),
        ]

        events.clear()
        project.resync_scope()
        assert events == [
            ("read_head", 1),
            ("load_scoped", 2),
            ("build_content_graph", 2),
            ("replace_content_index", 2),
            ("replace_graph_edges", 2),
            ("sync_reference_index", 2),
        ]
    finally:
        project.close()

    assert lock_depth == 0
