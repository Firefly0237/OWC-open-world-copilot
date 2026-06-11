"""Structured patch parser.

Only structured JSON is accepted — natural-language "fixes" are rejected by design. Within that
boundary the parser is tolerant of the op spellings and path shapes real models actually emit
(`set` for `replace`, missing leading slash, dotted paths), mirroring the QA-side lesson that
strictness belongs at the semantic layer (shadow validation), not the syntax layer.
"""

from __future__ import annotations

import json
from typing import Any

from .models import PatchCandidate

_OP_ALIASES = {
    "set": "replace",
    "update": "replace",
    "delete": "remove",
    "del": "remove",
}
_CANDIDATE_LIST_KEYS = ("candidates", "patches", "suggestions")


def parse_patch_candidates(raw: str) -> list[PatchCandidate]:
    data = _json_from_text(raw)
    if isinstance(data, dict):
        for key in _CANDIDATE_LIST_KEYS:
            if key in data:
                data = data[key]
                break
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise ValueError("patch output must be a JSON object, list, or {'candidates': [...]}")
    return [PatchCandidate.model_validate(_normalize_candidate(item)) for item in data]


def _normalize_candidate(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    normalized = dict(item)
    ops = normalized.get("ops")
    if isinstance(ops, list):
        normalized["ops"] = [_normalize_op(op) for op in ops]
    return normalized


def _normalize_op(op: Any) -> Any:
    if not isinstance(op, dict):
        return op
    normalized = dict(op)
    op_name = str(normalized.get("op", "")).strip().lower()
    normalized["op"] = _OP_ALIASES.get(op_name, op_name)
    path = normalized.get("path")
    if isinstance(path, str):
        normalized["path"] = _normalize_path(path)
    return normalized


def _normalize_path(path: str) -> str:
    cleaned = path.strip()
    if "/" not in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "/")
    if not cleaned.startswith("/"):
        cleaned = "/" + cleaned
    return cleaned


def _json_from_text(raw: str) -> Any:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return json.loads(text)
