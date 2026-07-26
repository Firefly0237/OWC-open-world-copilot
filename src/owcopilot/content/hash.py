# 稳定内容哈希（缓存命名空间/审计可复现/溯源）。
"""Stable content hashes for cache namespaces, audit reproducibility and traceability."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel


def _canonical_payload(value: Any) -> Any:
    """Canonicalize for hashing: dicts sort by key, lists keep their order.

    Lists are deliberately NOT sorted. Most lists in the content models are order-significant
    (``Quest.stages``, ``DialogueNode.choices``, story beats …) and reordering them would make two
    genuinely different worlds hash the same. The one list whose semantics are a *set* —
    ``ContentBundle.relations`` — is canonicalized by its own model's serializer
    (:func:`~owcopilot.content.models.relation_sort_key`), so it arrives here already ordered and
    this function stays free of any per-field special-casing.
    """
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda x: str(x[0]))
        return {str(k): _canonical_payload(v) for k, v in items}
    if isinstance(value, list):
        return [_canonical_payload(v) for v in value]
    if isinstance(value, tuple):
        return [_canonical_payload(v) for v in value]
    return value


def content_hash(value: Any) -> str:
    payload = _canonical_payload(value)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )
    return hashlib.sha256(raw).hexdigest()


def model_hash(model: BaseModel) -> str:
    return content_hash(model)
