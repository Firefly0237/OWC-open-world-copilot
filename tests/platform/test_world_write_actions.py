"""Canon-mutating app actions share the world-level read-modify-write lock."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

import owcopilot.app.actions.engine_sync as engine_sync_actions
import owcopilot.storage.world_lock as world_lock_module
from owcopilot.app.actions import (
    decide_review_action,
    run_apply_action,
    run_ingest_action,
    run_rollback_action,
)
from owcopilot.pipeline import ingest as ingest_pipeline
from owcopilot.pipeline import patches as patches_pipeline
from owcopilot.pipeline import review as review_pipeline


@pytest.mark.parametrize(
    ("action", "kwargs"),
    [
        (
            decide_review_action,
            {"item_id": "review-1", "decision": "accepted", "operator": "lead"},
        ),
        (run_apply_action, {"patch_id": "patch-1", "operator": "lead"}),
        (run_rollback_action, {"patch_id": "patch-1", "operator": "lead"}),
    ],
)
def test_canon_rmw_actions_enter_the_world_write_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: Callable[..., dict[str, Any]],
    kwargs: dict[str, Any],
) -> None:
    missing_root = tmp_path / "missing-content"
    locked_roots: list[Path] = []

    @contextmanager
    def observe_lock(content_root: str | Path) -> Iterator[None]:
        locked_roots.append(Path(content_root))
        yield

    monkeypatch.setattr(world_lock_module, "world_write_lock", observe_lock)

    with pytest.raises(FileNotFoundError):
        action(missing_root, **kwargs)

    assert locked_roots == [missing_root]


class _WorkflowBodyReached(RuntimeError):
    pass


@pytest.mark.parametrize(
    ("pipeline_module", "workflow", "kwargs"),
    [
        (
            review_pipeline,
            review_pipeline.decide_review_item,
            {"item_id": "review-1", "decision": "accepted", "operator": "lead"},
        ),
        (
            patches_pipeline,
            patches_pipeline.apply_patch_workflow,
            {"patch_id": "patch-1", "operator": "lead"},
        ),
        (
            patches_pipeline,
            patches_pipeline.rollback_patch_workflow,
            {"patch_id": "patch-1", "operator": "lead"},
        ),
        (ingest_pipeline, ingest_pipeline.run_ingest, {"paths": [], "dry_run": False}),
    ],
)
def test_direct_workflow_entry_enters_the_world_write_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pipeline_module: ModuleType,
    workflow: Callable[..., Any],
    kwargs: dict[str, Any],
) -> None:
    content_root = tmp_path / "content"
    locked_roots: list[Path] = []

    def stop_before_workflow_body() -> None:
        raise _WorkflowBodyReached

    project = SimpleNamespace(
        content_store=SimpleNamespace(root=content_root, read_head=stop_before_workflow_body),
        reload=stop_before_workflow_body,
    )

    @contextmanager
    def observe_lock(root: str | Path) -> Iterator[None]:
        locked_roots.append(Path(root))
        yield

    monkeypatch.setattr(pipeline_module, "world_write_lock", observe_lock)

    with pytest.raises(_WorkflowBodyReached):
        workflow(project, **kwargs)

    assert locked_roots == [content_root]


def test_ingest_action_commit_uses_the_pipeline_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content_root = tmp_path / "content"
    project = SimpleNamespace(content_store=SimpleNamespace(root=content_root))
    locked_roots: list[Path] = []

    @contextmanager
    def open_project(_root: str | Path, _sqlite_path: str | None) -> Iterator[Any]:
        yield project

    @contextmanager
    def observe_lock(root: str | Path) -> Iterator[None]:
        locked_roots.append(Path(root))
        yield

    def stop_before_workflow_body(*_args: Any, **_kwargs: Any) -> None:
        raise _WorkflowBodyReached

    monkeypatch.setattr(engine_sync_actions, "_project", open_project)
    monkeypatch.setattr(ingest_pipeline, "world_write_lock", observe_lock)
    monkeypatch.setattr(ingest_pipeline, "_run_ingest_unlocked", stop_before_workflow_body)

    with pytest.raises(_WorkflowBodyReached):
        run_ingest_action(content_root, paths=[], dry_run=False)

    assert locked_roots == [content_root]


def test_ingest_action_dry_run_does_not_take_the_world_write_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content_root = tmp_path / "content"
    project = SimpleNamespace(content_store=SimpleNamespace(root=content_root))
    calls: list[bool] = []

    @contextmanager
    def open_project(_root: str | Path, _sqlite_path: str | None) -> Iterator[Any]:
        yield project

    def unexpected_lock(_root: str | Path) -> None:
        raise AssertionError("dry-run ingest must not acquire the world write lock")

    def preview(*_args: Any, dry_run: bool, **_kwargs: Any) -> Any:
        calls.append(dry_run)
        return SimpleNamespace(
            dry_run=True,
            incoming_count=0,
            changes=[],
            issues=[],
            has_errors=False,
            content_hash_before="unchanged",
            content_hash_after="unchanged",
        )

    monkeypatch.setattr(engine_sync_actions, "_project", open_project)
    monkeypatch.setattr(ingest_pipeline, "world_write_lock", unexpected_lock)
    monkeypatch.setattr(ingest_pipeline, "_run_ingest_unlocked", preview)

    result = run_ingest_action(content_root, paths=[], dry_run=True)

    assert result["dry_run"] is True
    assert calls == [True]
