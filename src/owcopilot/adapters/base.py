"""Quest coercion helpers shared by the engine-import path (engine rows → v2 Quest → review)."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any

from ..content.models import Quest, Reward


def slug(text: str) -> str:
    """ASCII id from a title. Pure-CJK (or otherwise non-ASCII) titles slug to empty, so fall back
    to a stable content hash — otherwise every Chinese-titled quest collapses to one id and engine
    rows silently overwrite each other on import. Deterministic: the same title always maps to the
    same id, keeping the historical title-derived FName stable for ASCII titles."""
    base = re.sub(r"[^a-z0-9]+", "_", (text or "").strip().lower()).strip("_")
    if base:
        return base
    stripped = (text or "").strip()
    if stripped:
        return "untitled_" + hashlib.sha1(stripped.encode("utf-8")).hexdigest()[:8]
    return "untitled"


def coerce_quest(artifact: Mapping[str, Any]) -> Quest:
    """Normalize a (possibly pre-v2) quest dict into a v2 `Quest`, shared by every engine adapter so
    they all land the one v2 schema. The legacy core speaks a single `reward` string and carries no
    stable `id`; fold both, so a legacy draft and a v2 Quest produce the identical engine row."""
    data: dict[str, Any] = dict(artifact)
    if "rewards" not in data and data.get("reward"):
        data["rewards"] = [Reward(kind="reward", value=str(data["reward"]))]
    data.pop("reward", None)
    # A title-derived id keeps the historical (title-based) asset name: the id is slugged downstream
    # and slugging is idempotent, so `Quest_smoke_over_the_marsh` is unchanged.
    data.setdefault("id", slug(str(data.get("title", ""))))
    return Quest.model_validate(data)
