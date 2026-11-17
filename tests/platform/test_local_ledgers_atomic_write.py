"""Atomic-writer inventory and corruption behavior for HTTP-reachable local ledgers."""

from __future__ import annotations

import ast
import importlib
import inspect
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from owcopilot.app.actions.engine_sync import list_mapping_templates_action
from owcopilot.assets import AssetStore
from owcopilot.assist.sweep import SweepExclusionStore
from owcopilot.collab.store import CollabStore
from owcopilot.compliance.store import CaseStore
from owcopilot.content.snapshot import list_snapshots
from owcopilot.content.store import ContentStore
from owcopilot.inspiration.store import ReferenceStore
from owcopilot.localization import LocStore
from owcopilot.storage.atomic_file import CorruptJSONError


def _load_assets(root: Path) -> Any:
    return AssetStore(root).load()


def _load_collab(root: Path) -> Any:
    return CollabStore(root).load()


def _load_localization(root: Path) -> Any:
    return LocStore(root).load()


def _load_compliance(root: Path) -> Any:
    return CaseStore(root).load()


def _load_sweep(root: Path) -> Any:
    return SweepExclusionStore(root).load()


def _load_mappings(root: Path) -> Any:
    return list_mapping_templates_action(root)


def _load_snapshots(root: Path) -> Any:
    return list_snapshots(ContentStore(root))


def _load_references(root: Path) -> Any:
    return ReferenceStore(root).list_sources()


_CORRUPTION_CASES: list[tuple[str, Callable[[Path], Any]]] = [
    (".assets/assets.json", _load_assets),
    (".collab/collab.json", _load_collab),
    (".localization/status.json", _load_localization),
    (".compliance/cases.json", _load_compliance),
    (".sweep/exclusions.json", _load_sweep),
    (".owcopilot/recognize_mappings.json", _load_mappings),
    (".snapshots/20260803.json", _load_snapshots),
    ("references/sources/ref_broken.json", _load_references),
]


@pytest.mark.parametrize(("relative_path", "load"), _CORRUPTION_CASES)
def test_damaged_json_is_never_treated_as_empty_state(
    tmp_path: Path, relative_path: str, load: Callable[[Path], Any]
) -> None:
    path = tmp_path / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    damaged = '{\n  "kept": true,\n  broken\n}'
    path.write_text(damaged, encoding="utf-8")

    with pytest.raises(CorruptJSONError) as caught:
        load(tmp_path)

    assert caught.value.path == path.resolve()
    assert caught.value.line == 3
    assert caught.value.column == 3
    assert str(path.resolve()) in str(caught.value)
    assert "line 3" in str(caught.value) and "column 3" in str(caught.value)
    assert path.read_text(encoding="utf-8") == damaged


@pytest.mark.parametrize(
    ("relative_path", "load", "message"),
    [
        (".owcopilot/recognize_mappings.json", _load_mappings, "JSON object"),
        (".sweep/exclusions.json", _load_sweep, "JSON object"),
    ],
)
def test_valid_non_object_ledger_is_not_silently_replaced_with_empty_state(
    tmp_path: Path,
    relative_path: str,
    load: Callable[[Path], Any],
    message: str,
) -> None:
    path = tmp_path / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match=message) as caught:
        load(tmp_path)

    assert str(path.resolve()) in str(caught.value)
    assert path.read_text(encoding="utf-8") == "[]"


def test_scoped_local_writer_inventory_has_no_naked_write_text() -> None:
    # The two write-once stores are intentionally included: a torn snapshot poisons the glob read,
    # and torn inspiration metadata/units poison retrieval (while torn raw text corrupts chunking).
    expected_atomic_calls = {
        "owcopilot.assets": 1,
        "owcopilot.collab.store": 1,
        "owcopilot.localization": 1,
        "owcopilot.compliance.store": 1,
        "owcopilot.assist.sweep": 1,
        "owcopilot.app.actions.engine_sync": 1,
        "owcopilot.content.snapshot": 1,
        # text source/meta/unit writes plus the quarantine-approval metadata rewrite
        "owcopilot.inspiration.store": 6,
    }

    for module_name, expected_count in expected_atomic_calls.items():
        module = importlib.import_module(module_name)
        tree = ast.parse(inspect.getsource(module))
        naked = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "write_text"
        ]
        atomic_count = sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "atomic_write_text"
        )
        assert naked == [], f"{module_name} has naked write_text at lines {naked}"
        assert atomic_count == expected_count, module_name

    inspiration_module = importlib.import_module("owcopilot.inspiration.store")
    inspiration_tree = ast.parse(inspect.getsource(inspiration_module))
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "atomic_write_bytes"
        for node in ast.walk(inspiration_tree)
    ), "immutable uploaded artifacts must use the crash-safe binary writer"
