"""Apply patch operations to a shadow ContentBundle copy."""

from __future__ import annotations

from typing import Any

from ..content.models import ContentBundle
from .models import PatchOp, PatchOperation


def apply_patch_shadow(bundle: ContentBundle, ops: list[PatchOperation]) -> ContentBundle:
    data = bundle.model_dump(mode="json")
    for op in ops:
        _apply_op(data, op)
    return ContentBundle.model_validate(data)


def _apply_op(document: dict[str, Any], op: PatchOperation) -> None:
    parent, key = _resolve_parent(document, op.path)
    if op.op in {PatchOp.ADD, PatchOp.REPLACE}:
        _set(parent, key, op.value)
        return
    if op.op is PatchOp.REMOVE:
        _remove(parent, key)
        return
    raise ValueError(f"unsupported patch op: {op.op}")


def _resolve_parent(document: Any, path: str) -> tuple[Any, str]:
    parts = _parts(path)
    if not parts:
        raise ValueError("patch path must not be empty")
    current = document
    for part in parts[:-1]:
        current = _get(current, part)
    return current, parts[-1]


def _parts(path: str) -> list[str]:
    if not path.startswith("/"):
        raise ValueError(f"patch path must be a JSON pointer: {path!r}")
    return [part.replace("~1", "/").replace("~0", "~") for part in path.split("/")[1:]]


def _get(value: Any, key: str) -> Any:
    if isinstance(value, list):
        return value[int(key)]
    if isinstance(value, dict):
        return value[key]
    raise ValueError(f"cannot traverse into {type(value).__name__}")


def _set(parent: Any, key: str, value: Any) -> None:
    if isinstance(parent, list):
        if key == "-":
            parent.append(value)
        else:
            parent[int(key)] = value
        return
    if isinstance(parent, dict):
        parent[key] = value
        return
    raise ValueError(f"cannot set on {type(parent).__name__}")


def _remove(parent: Any, key: str) -> None:
    if isinstance(parent, list):
        del parent[int(key)]
        return
    if isinstance(parent, dict):
        del parent[key]
        return
    raise ValueError(f"cannot remove from {type(parent).__name__}")
