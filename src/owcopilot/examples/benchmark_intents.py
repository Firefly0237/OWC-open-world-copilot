"""A fixed workload for the P2 cost benchmark — the 'ruler' we measure every optimisation against.

The set deliberately mixes (against the in-code demo World Bible):
  * normal      — straightforward, consistent requests across different NPCs/locations.
  * faction/timeline/unknown — 'hard' requests where a CHEAP model would plausibly produce a
    lore-breaking quest (so the CascadeRouter has a reason to escalate); the STRONG model gets
    them right. This exercises "validators as the cascade's confidence signal" (T5).
  * duplicate   — byte-identical repeats of earlier intents -> exercise the L1 ExactCache.
  * paraphrase  — same request, reordered + a filler word -> L1 misses, L2 SemanticCache hits.

`BenchmarkProvider` is the offline stand-in: it returns a per-intent quest, handing the CHEAP
tier the deliberately-inconsistent variant on the hard intents and the STRONG tier a consistent
one. That makes the whole benchmark deterministic and $0 while still driving real escalations
and repairs. Swap it for OpenAICompatProvider via the benchmark's `--real` flag.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


def _quest(
    title: str,
    npc: str,
    loc: str,
    objective: str,
    reward: str = "50 gold",
    prerequisites: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "title": title,
        "giver_npc": npc,
        "location": loc,
        "objective": objective,
        "reward": reward,
        "prerequisites": list(prerequisites or []),
    }


@dataclass
class BenchmarkIntent:
    intent: str
    kind: str  # normal|faction|timeline|unknown|duplicate|paraphrase
    cheap: dict[str, Any]  # what the CHEAP tier returns offline
    strong: dict[str, Any] = field(default_factory=dict)  # STRONG tier (defaults to == cheap)

    def __post_init__(self) -> None:
        if not self.strong:
            self.strong = self.cheap


# --- 5 normal (consistent on both tiers; distinct NPC/location mixes so they don't collide) ---
N1 = BenchmarkIntent(
    "Write a supply-run quest for Aldric escorting a caravan to Northwatch.",
    "normal",
    _quest("The Northern Supply Run", "Aldric", "Northwatch", "Escort the caravan to Northwatch"),
)
N2 = BenchmarkIntent(
    "Create a quest where Mira gathers healing herbs near Riverbend.",
    "normal",
    _quest("Herbs for the Wounded", "Mira", "Riverbend", "Gather healing herbs near Riverbend"),
)
N3 = BenchmarkIntent(
    "Design a patrol quest for Aldric guarding the road into Riverbend.",
    "normal",
    _quest("Road Watch", "Aldric", "Riverbend", "Patrol the road into Riverbend"),
)
N4 = BenchmarkIntent(
    "Give Mira a quest tending wounded travellers in Northwatch.",
    "normal",
    _quest("Mercy at the Gate", "Mira", "Northwatch", "Tend wounded travellers in Northwatch"),
)
N5 = BenchmarkIntent(
    "Write a quest where Garruk fortifies the Marsh Reavers camp in Shadowfen.",
    "normal",
    _quest("Hold the Marsh", "Garruk", "Shadowfen", "Fortify the Reaver camp in Shadowfen"),
)

# --- 4 hard: CHEAP breaks lore, STRONG fixes it (drives cascade escalation) ---
H1 = BenchmarkIntent(
    "Send Aldric to raid the Marsh Reavers hideout inside Shadowfen.",
    "faction",
    cheap=_quest("Into the Marsh", "Aldric", "Shadowfen", "Strike the Reaver hideout in Shadowfen"),
    strong=_quest("Into the Marsh", "Aldric", "Northwatch", "Stage the strike from Northwatch"),
)
H2 = BenchmarkIntent(
    "Have Mira carry medicine into Shadowfen to treat injured travellers.",
    "faction",
    cheap=_quest("Mercy in the Mire", "Mira", "Shadowfen", "Carry medicine into Shadowfen"),
    strong=_quest("Mercy in the Mire", "Mira", "Riverbend", "Treat the injured at Riverbend"),
)
H3 = BenchmarkIntent(
    "Write the quest 'The Caravan Ambush' that lists the Siege of Northwatch as a prerequisite.",
    "timeline",
    cheap=_quest(
        "The Caravan Ambush",
        "Aldric",
        "Northwatch",
        "Survive the ambush",
        prerequisites=["The Siege of Northwatch"],
    ),
    strong=_quest(
        "The Caravan Ambush", "Aldric", "Northwatch", "Survive the ambush", prerequisites=[]
    ),
)
H4 = BenchmarkIntent(
    "Create a treasure-hunt quest for Aldric hidden in the ruins of Atlantis.",
    "unknown",
    cheap=_quest("Lost Riches", "Aldric", "Atlantis", "Recover the lost treasure of Atlantis"),
    strong=_quest(
        "Lost Riches", "Aldric", "Northwatch", "Recover the treasure cached at Northwatch"
    ),
)

# --- 3 duplicates (exact repeats -> L1 ExactCache) ---
D1 = BenchmarkIntent(N1.intent, "duplicate", N1.cheap, N1.strong)
D2 = BenchmarkIntent(N2.intent, "duplicate", N2.cheap, N2.strong)
D3 = BenchmarkIntent(N3.intent, "duplicate", N3.cheap, N3.strong)

# --- 2 paraphrases (same words reordered + a filler -> L1 misses, L2 SemanticCache hits) ---
P1 = BenchmarkIntent(
    "For Aldric, please write a supply-run quest escorting a caravan to Northwatch.",
    "paraphrase",
    N1.cheap,
    N1.strong,
)
P2 = BenchmarkIntent(
    "In Northwatch, please give Mira a quest tending wounded travellers.",
    "paraphrase",
    N4.cheap,
    N4.strong,
)

# Order matters: every duplicate/paraphrase comes AFTER its base so the cache is warm.
BENCHMARK_INTENTS: list[BenchmarkIntent] = [N1, N2, N3, N4, N5, H1, H2, H3, H4, D1, D2, D3, P1, P2]


def intent_texts(intents: list[BenchmarkIntent] | None = None) -> list[str]:
    return [bi.intent for bi in (intents or BENCHMARK_INTENTS)]


def scenarios(intents: list[BenchmarkIntent] | None = None) -> dict[str, BenchmarkIntent]:
    """Map a generate prompt's `user` (== the intent text) to its scenario."""
    return {bi.intent: bi for bi in (intents or BENCHMARK_INTENTS)}


_SAFE_FIX = _quest(
    "Repaired Quest", "Aldric", "Northwatch", "Carry out the task safely", "50 gold", []
)


class BenchmarkProvider:
    """Deterministic offline provider for the benchmark.

    * generate: look the intent up in `scenarios` and return the CHEAP or STRONG variant for
      the routed tier (the 4-tuple omits cached tokens, so offline provider-cache share == 0,
      as the spec requires — that number only becomes real with `--real`).
    * repair  : return a canonical lore-safe quest (a backstop; with STRONG always consistent
      the verify->repair path is rarely reached in the benchmark).
    """

    REPAIR_MARKER = "TASK: REPAIR"

    def __init__(self, scenario_map: dict[str, BenchmarkIntent], *, cheap_tier: str = "cheap"):
        self.scenario_map = scenario_map
        self.cheap_tier = cheap_tier

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        if self.REPAIR_MARKER in system:
            quest = _SAFE_FIX
        else:
            bi = self.scenario_map.get(user)
            quest = (bi.cheap if model == self.cheap_tier else bi.strong) if bi else _SAFE_FIX
        text = json.dumps(quest)
        in_tok = max(1, (len(system) + len(user)) // 4)
        out_tok = max(1, len(text) // 4)
        return text, in_tok, out_tok
