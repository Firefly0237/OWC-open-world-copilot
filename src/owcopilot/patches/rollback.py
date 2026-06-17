"""Patch rollback helpers based on inverse JSON Patch operations."""

from __future__ import annotations

from typing import Any

from ..content.models import ContentBundle
from .models import PatchOp, PatchOperation
from .pointer import pointer_parts


def inverse_operations(
    bundle_before: ContentBundle, ops: list[PatchOperation]
) -> list[PatchOperation]:
    document: Any = bundle_before.model_dump(mode="json")
    inverses: list[PatchOperation] = []
    for op in ops:
        if op.op is PatchOp.ADD:
            inverses.append(
                PatchOperation(op=PatchOp.REMOVE, path=_actual_add_path(document, op.path))
            )
            _apply_to_document(document, op)
        elif op.op is PatchOp.REPLACE:
            old_value = _get_path(document, op.path)
            inverses.append(PatchOperation(op=PatchOp.REPLACE, path=op.path, value=old_value))
            _apply_to_document(document, op)
        elif op.op is PatchOp.REMOVE:
            old_value = _get_path(document, op.path)
            inverses.append(PatchOperation(op=PatchOp.ADD, path=op.path, value=old_value))
            _apply_to_document(document, op)
    return list(reversed(inverses))


def _get_path(document: Any, path: str) -> Any:
    current = document
    for part in pointer_parts(path):
        if isinstance(current, list):
            current = current[int(part)]
        elif isinstance(current, dict):
            current = current[part]
        else:
            raise ValueError(f"cannot traverse into {type(current).__name__}")
    return current


def _apply_to_document(document: Any, op: PatchOperation) -> None:
    parent, key = _resolve_parent(document, op.path)
    if op.op in {PatchOp.ADD, PatchOp.REPLACE}:
        if isinstance(parent, list):
            if key == "-":
                parent.append(op.value)
            else:
                parent[int(key)] = op.value
        else:
            parent[key] = op.value
    elif op.op is PatchOp.REMOVE:
        if isinstance(parent, list):
            del parent[int(key)]
        else:
            del parent[key]


def _resolve_parent(document: Any, path: str) -> tuple[Any, str]:
    parts = pointer_parts(path)
    current = document
    for part in parts[:-1]:
        if isinstance(current, list):
            current = current[int(part)]
        else:
            current = current[part]
    return current, parts[-1]


def _actual_add_path(document: Any, path: str) -> str:
    parent, key = _resolve_parent(document, path)
    if isinstance(parent, list) and key == "-":
        return _replace_last_pointer_part(path, str(len(parent)))
    return path


def _replace_last_pointer_part(path: str, new_part: str) -> str:
    parts = path.split("/")
    parts[-1] = new_part.replace("~", "~0").replace("/", "~1")
    return "/".join(parts)
