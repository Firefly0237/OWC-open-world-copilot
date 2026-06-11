"""Deterministic offline providers for assist tasks.

Like `qa.offline.OfflineQAProvider`, these are not language models: they emit minimal structured
output so the surrounding machinery — constrained prompts, parsing, audit, lint, review queue —
can be exercised end-to-end at $0. Swap in `OpenAICompatProvider` (`--llm-mode real`) for real
generation; nothing else in the flow changes.
"""

from __future__ import annotations

import json
import re

from ..content.normalize import slug_id


class OfflineQuestDraftProvider:
    """Return a minimal, reference-free quest draft built from the brief itself."""

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        brief = user.strip() or "untitled quest"
        quest_id = slug_id(brief[:48], prefix="quest")
        payload = {
            "id": quest_id,
            "title": brief[:60],
            "objective": brief,
            "localization_keys": [f"quest.{quest_id}.objective"],
        }
        text = json.dumps(payload, ensure_ascii=False)
        return text, max(1, (len(system) + len(user)) // 4), max(1, len(text) // 4)


class OfflineBarksProvider:
    """Return deterministic short bark variants that respect the requested count and length."""

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        count = _requested_variants(user)
        max_chars = _max_chars(system)
        name = _voice_name(system)
        topic = _topic(user)
        variants = []
        for index in range(1, count + 1):
            text = f"{name}: {topic} ({index})" if name else f"{topic} ({index})"
            variants.append(text[:max_chars])
        text = json.dumps({"variants": variants}, ensure_ascii=False)
        return text, max(1, (len(system) + len(user)) // 4), max(1, len(text) // 4)


def _requested_variants(user: str) -> int:
    match = re.search(r"Variants:\s*(\d+)", user)
    return max(1, int(match.group(1))) if match else 1


def _max_chars(system: str) -> int:
    match = re.search(r"<=\s*(\d+)\s*characters", system)
    return max(8, int(match.group(1))) if match else 40


def _voice_name(system: str) -> str:
    match = re.search(r'"name"\s*:\s*"([^"]*)"', system)
    return match.group(1) if match else ""


def _topic(user: str) -> str:
    match = re.search(r"Topic:\s*(.+)", user)
    return match.group(1).strip() if match else user.strip()
