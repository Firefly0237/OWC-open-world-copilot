"""Stable content hashes for cache namespaces, audit reproducibility and traceability."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel


def _canonical_payload(value: Any) -> Any:
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
