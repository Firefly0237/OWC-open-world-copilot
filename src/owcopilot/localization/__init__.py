"""WS-F · localization workflow: string status + assignment + coverage.

Beyond the existing localization *export*, this tracks each string through 待译→已译→待校→定稿,
assigns it, and shows coverage (which locales are missing which keys). State lives in a per-world
JSON ledger (zero canon pollution); term-glossary consistency is already covered by the term audit.
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..content.models import ContentBundle


class LocStatus(str, Enum):
    UNTRANSLATED = "untranslated"  # 待译
    TRANSLATED = "translated"  # 已译
    REVIEWING = "reviewing"  # 待校
    FINAL = "final"  # 定稿


_FLOW: dict[LocStatus, set[LocStatus]] = {
    LocStatus.UNTRANSLATED: {LocStatus.TRANSLATED},
    LocStatus.TRANSLATED: {LocStatus.REVIEWING, LocStatus.UNTRANSLATED},
    LocStatus.REVIEWING: {LocStatus.FINAL, LocStatus.TRANSLATED},
    LocStatus.FINAL: {LocStatus.REVIEWING},
}


class LocEntry(BaseModel):
    text_key: str
    locale: str
    status: LocStatus
    assignee: str = ""


class LocState(BaseModel):
    entries: dict[str, LocEntry] = Field(default_factory=dict)  # "<key>|<locale>" -> entry


def _slot(text_key: str, locale: str) -> str:
    return f"{text_key}|{locale}"


def _present_pairs(bundle: ContentBundle) -> dict[str, set[str]]:
    """text_key -> set of locales that actually have a string."""
    present: dict[str, set[str]] = {}
    for text in bundle.localized_texts.values():
        present.setdefault(text.text_key, set()).add(text.locale)
    for dialogue in bundle.dialogues.values():
        if dialogue.text and dialogue.locale:
            present.setdefault(dialogue.text_key, set()).add(dialogue.locale)
    return present


def status_of(state: LocState, text_key: str, locale: str, *, present: bool) -> LocStatus:
    entry = state.entries.get(_slot(text_key, locale))
    if entry is not None:
        return entry.status
    return LocStatus.TRANSLATED if present else LocStatus.UNTRANSLATED


def transition(state: LocState, *, text_key: str, locale: str, to: LocStatus, by: str) -> LocEntry:
    if not by.strip():
        raise ValueError("请先填写署名")
    current = status_of(state, text_key, locale, present=True)
    if to not in _FLOW.get(current, set()):
        raise ValueError(f"不允许的流转：{current.value} → {to.value}")
    slot = _slot(text_key, locale)
    existing = state.entries.get(slot)
    entry = LocEntry(
        text_key=text_key,
        locale=locale,
        status=to,
        assignee=existing.assignee if existing else "",
    )
    state.entries[slot] = entry
    return entry


def assign(state: LocState, *, text_key: str, locale: str, assignee: str) -> LocEntry:
    slot = _slot(text_key, locale)
    existing = state.entries.get(slot)
    entry = LocEntry(
        text_key=text_key,
        locale=locale,
        status=existing.status if existing else LocStatus.UNTRANSLATED,
        assignee=assignee.strip(),
    )
    state.entries[slot] = entry
    return entry


def build_localization_overview(
    bundle: ContentBundle, state: LocState, *, locales: list[str] | None = None
) -> dict[str, Any]:
    present = _present_pairs(bundle)
    seen_locales = sorted({loc for locs in present.values() for loc in locs})
    targets = sorted(set(locales)) if locales else seen_locales
    by_status: dict[str, int] = {}
    rows: list[dict[str, Any]] = []
    missing_total = 0
    for text_key in sorted(present):
        here = present[text_key]
        per_locale = {}
        for locale in targets:
            is_present = locale in here
            st = status_of(state, text_key, locale, present=is_present)
            per_locale[locale] = st.value
            by_status[st.value] = by_status.get(st.value, 0) + 1
            if not is_present:
                missing_total += 1
        rows.append(
            {
                "text_key": text_key,
                "present_locales": sorted(here),
                "missing_locales": [loc for loc in targets if loc not in here],
                "status": per_locale,
            }
        )
    return {
        "locales": targets,
        "keys": len(present),
        "by_status": by_status,
        "missing_total": missing_total,
        "rows": rows,
    }


class LocStore:
    def __init__(self, world_root: str | Path) -> None:
        self.path = Path(world_root) / ".localization" / "status.json"

    def load(self) -> LocState:
        if not self.path.exists():
            return LocState()
        return LocState.model_validate_json(self.path.read_text(encoding="utf-8"))

    def save(self, state: LocState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(state.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )


__all__ = [
    "LocEntry",
    "LocState",
    "LocStatus",
    "LocStore",
    "assign",
    "build_localization_overview",
    "status_of",
    "transition",
]
