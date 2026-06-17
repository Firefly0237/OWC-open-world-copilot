"""Offline test doubles, collected in one place.

These are the deterministic, $0 stand-ins that let the whole pipeline — generation and the
verify→repair loop — run offline in tests, demos and the benchmark without any API key. They are
kept *out* of the production modules (`llm/gateway.py`) so those files contain only real
implementations; each production module re-exports the relevant double from here for compatibility.

Note: `HashingEmbedder` is **not** a test double — it is the real, dependency-free default embedder
for `SemanticCache` and therefore stays in `llm/cache.py`. `BenchmarkProvider` stays in
`examples/benchmark_intents.py` because it is tightly coupled to that benchmark fixture data.

  Providers (implement the structural `llm.gateway.LLMProvider` protocol):
    - MockProvider          — echoes the prompt; used by P0 and the cheap planner tier.
    - StructuredFakeProvider — returns a fixed, World-Bible-grounded Quest as JSON.
    - ScriptedFakeProvider   — returns different quests for generation vs repair (keys on a marker).
"""

from __future__ import annotations

import json


# --------------------------------------------------------------------------- LLM providers
class MockProvider:
    """Deterministic, offline provider for tests & P0. Token counts approximated as len/4."""

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        text = f"[mock:{model}] " + (user[:60] if user else "")
        in_tok = max(1, (len(system) + len(user)) // 4)
        out_tok = max(1, len(text) // 4)
        return text, in_tok, out_tok


class StructuredFakeProvider:
    """Offline stand-in for a real model running in JSON / function-calling mode.

    Returns a deterministic, World-Bible-grounded Quest as a JSON string so the P1
    grounded pipeline runs end-to-end without API keys. Swap for OpenAICompatProvider
    once you have a real endpoint.
    """

    DEFAULT_QUEST = {
        "title": "The Northern Supply Run",
        "giver_npc": "Aldric",
        "location": "Northwatch",
        "objective": "Escort Aldric's caravan through the pass to Northwatch before nightfall",
        "reward": "75 gold",
        "prerequisites": [],
    }

    def __init__(self, quest: dict | None = None):
        self.quest = quest or dict(self.DEFAULT_QUEST)

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        text = json.dumps(self.quest)
        in_tok = max(1, (len(system) + len(user)) // 4)  # grounded prompt -> realistic input cost
        out_tok = max(1, len(text) // 4)
        return text, in_tok, out_tok


class ScriptedFakeProvider:
    """Offline stand-in that returns DIFFERENT structured quests for generation vs repair.

    A single gateway routes both `generate` and `repair` to the frontier tier, so one
    provider instance serves both. It tells them apart by a stable marker the
    `LLMRepairStrategy` places in its system prompt (`TASK: REPAIR`): repair prompts get the
    corrected quest, everything else gets the (deliberately inconsistent) generation quest.

    This lets the milestone demo run the full intent -> generate -> catch -> repair -> clean
    loop deterministically at $0. Swap for OpenAICompatProvider to go live.
    """

    REPAIR_MARKER = "TASK: REPAIR"

    def __init__(self, *, generate: dict, repair: dict):
        self.generate = generate
        self.repair = repair

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        quest = self.repair if self.REPAIR_MARKER in system else self.generate
        text = json.dumps(quest)
        in_tok = max(1, (len(system) + len(user)) // 4)
        out_tok = max(1, len(text) // 4)
        return text, in_tok, out_tok


__all__ = [
    "MockProvider",
    "StructuredFakeProvider",
    "ScriptedFakeProvider",
]
