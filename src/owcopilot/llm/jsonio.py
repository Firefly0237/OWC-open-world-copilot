"""Tolerant extraction of JSON from model replies.

Real models wrap their JSON in prose ("Here is the world: { … }") or markdown fences, or add a
trailing sentence. A parser that does ``json.loads(reply)`` and only strips fences crashes on output
it should have handled — a recurring class of bug across every generation surface. These helpers
pull the first balanced object/array out of the reply instead. A genuinely unparseable reply raises
``ValueError`` so the caller can retry or surface the failure, never crash mid-pipeline.
"""

from __future__ import annotations

import json
from typing import Any


def extract_json_object(raw: str) -> dict[str, Any]:
    """Pull the first balanced ``{ … }`` object out of a model reply.

    Raises ``ValueError`` when no object is present or the JSON is invalid."""
    value = _extract(raw, "{", "}")
    if not isinstance(value, dict):
        raise ValueError("model reply did not contain a JSON object")
    return value


def extract_json(raw: str) -> Any:
    """Pull the first balanced JSON object OR array out of a model reply.

    Some prompts ask for a top-level array (e.g. a list of bark lines). Raises ``ValueError`` when
    neither an object nor an array is found, or the JSON is invalid."""
    text = raw.strip()
    obj_at = text.find("{")
    arr_at = text.find("[")
    if obj_at == -1 and arr_at == -1:
        raise ValueError("model reply contained no JSON")
    if arr_at != -1 and (obj_at == -1 or arr_at < obj_at):
        return _extract(raw, "[", "]")
    return _extract(raw, "{", "}")


def _extract(raw: str, open_ch: str, close_ch: str) -> Any:
    text = raw.strip()
    start = text.find(open_ch)
    end = text.rfind(close_ch)
    if start == -1 or end <= start:
        raise ValueError("no balanced JSON span found in model reply")
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"model reply was not valid JSON: {exc}") from exc
