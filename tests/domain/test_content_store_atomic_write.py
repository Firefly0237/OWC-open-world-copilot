"""ContentStore single-file atomicity and writer-inventory invariants."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

import owcopilot.content.store as store_module
from owcopilot.content.models import Entity, EntityType
from owcopilot.content.store import ContentStore
from owcopilot.storage.atomic_file import CorruptJSONError


def _entity(entity_id: str) -> Entity:
    return Entity(id=entity_id, name=entity_id, type=EntityType.NPC)


def test_content_store_all_ten_single_file_writers_use_shared_atomic_helper() -> None:
    tree = ast.parse(inspect.getsource(store_module))
    expected = {
        "freeze_version": 1,
        "_write_head": 1,
        "create_version": 1,
        "save_scoped": 1,
        "_write_relations": 1,
        "_write_quest_event_refs": 1,
        "_write_terms": 1,
        "_write_style_guides": 1,
        "_write_style_rubrics": 1,
        "_write_json": 1,
    }
    actual: dict[str, int] = {}
    naked_writes: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        atomic_calls = 0
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            if isinstance(child.func, ast.Name) and child.func.id == "atomic_write_text":
                atomic_calls += 1
            if isinstance(child.func, ast.Attribute) and child.func.attr == "write_text":
                naked_writes.append(child.lineno)
        if atomic_calls:
            actual[node.name] = atomic_calls

    assert naked_writes == []
    assert actual == expected
    assert sum(actual.values()) == 10


@pytest.mark.parametrize("failure_phase", ["before", "after"])
def test_object_rename_failure_never_loses_both_old_and_new_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_phase: str
) -> None:
    store = ContentStore(tmp_path)
    directory = tmp_path / "world" / "entities"
    store._write_json_dir(directory, {"old": _entity("old")})
    original_write = store._write_json

    def injected_write(path: Path, model: Entity) -> None:
        if path.name == "new.json":
            if failure_phase == "after":
                original_write(path, model)
            raise OSError(f"injected {failure_phase} publish failure")
        original_write(path, model)

    monkeypatch.setattr(store, "_write_json", injected_write)
    with pytest.raises(OSError, match=failure_phase):
        store._write_json_dir(directory, {"new": _entity("new")})

    old_path = directory / "old.json"
    new_path = directory / "new.json"
    assert old_path.exists() or new_path.exists()
    assert old_path.exists()  # stale deletion starts only after every expected write succeeds
    if new_path.exists():
        assert Entity.model_validate_json(new_path.read_text(encoding="utf-8")).id == "new"


def test_json_directory_writes_every_new_file_before_any_stale_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ContentStore(tmp_path)
    directory = tmp_path / "world" / "entities"
    store._write_json_dir(directory, {"old": _entity("old")})
    original_write = store._write_json

    def fail_on_second(path: Path, model: Entity) -> None:
        if path.name == "new_b.json":
            raise OSError("second expected file failed")
        original_write(path, model)

    monkeypatch.setattr(store, "_write_json", fail_on_second)
    with pytest.raises(OSError, match="second expected"):
        store._write_json_dir(
            directory,
            {"new_a": _entity("new_a"), "new_b": _entity("new_b")},
        )

    assert (directory / "old.json").exists()
    assert (directory / "new_a.json").exists()
    assert not (directory / "new_b.json").exists()


def test_successful_object_rename_publishes_new_then_removes_old(tmp_path: Path) -> None:
    store = ContentStore(tmp_path)
    directory = tmp_path / "world" / "entities"
    store._write_json_dir(directory, {"old": _entity("old")})
    store._write_json_dir(directory, {"new": _entity("new")})

    assert not (directory / "old.json").exists()
    assert (directory / "new.json").exists()


def test_corrupt_content_json_reports_absolute_path_line_and_column(tmp_path: Path) -> None:
    path = tmp_path / "world" / "entities" / "broken.json"
    path.parent.mkdir(parents=True)
    path.write_text('{\n  "id": "broken",\n  invalid\n}', encoding="utf-8")

    with pytest.raises(CorruptJSONError) as caught:
        ContentStore(tmp_path).load()

    error = caught.value
    assert error.path == path.resolve()
    assert error.line == 3
    assert error.column == 3
    assert str(path.resolve()) in str(error)
    assert "未将其当作空数据" in str(error)


def test_corrupt_jsonl_reports_the_real_file_line(tmp_path: Path) -> None:
    path = tmp_path / "world" / "relations.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text('{"source":"a","target":"b","kind":"knows"}\n{broken\n', encoding="utf-8")

    with pytest.raises(CorruptJSONError) as caught:
        ContentStore(tmp_path).load()

    assert caught.value.path == path.resolve()
    assert caught.value.line == 2
    assert caught.value.column == 2
