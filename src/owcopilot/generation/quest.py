"""Quest generation.

The Quest schema is the contract the model must satisfy — generating *structured* output
instead of free text is the first line of defence against hallucination. The
GroundedQuestGenerator adds the second: it retrieves only the relevant lore sub-graph and
puts it in the prompt, so the model references real entities (and we send fewer tokens
than dumping the whole World Bible).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from typing import Any

from pydantic import BaseModel, Field, field_validator

from ..llm.gateway import LLMGateway
from ..worldbible.graph import LoreGraph
from ..worldbible.models import WorldBible

_NONE_PREREQ = {"", "none", "n/a", "na", "no prerequisites", "-", "null"}
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))


class Quest(BaseModel):
    title: str
    giver_npc: str  # must reference a World Bible NPC
    location: str  # must reference a World Bible Location
    objective: str
    reward: str = ""
    prerequisites: list[str] = Field(default_factory=list)
    timeline_order: int | None = None

    @field_validator("prerequisites", mode="before")
    @classmethod
    def _coerce_prerequisites(cls, v: Any) -> Any:
        """Real models (even in JSON mode) sometimes return prerequisites as a prose string
        or null instead of an array. Normalise to a clean list so structured generation
        stays robust without a brittle re-prompt."""
        if v is None:
            return []
        if isinstance(v, str):
            if v.strip().lower() in _NONE_PREREQ:
                return []
            parts = [p.strip(" -•\t") for p in re.split(r"[;\n]+", v)]
            return [p for p in parts if p]
        return v

    @field_validator("reward", mode="before")
    @classmethod
    def _coerce_reward(cls, v: Any) -> Any:
        """Tolerate a numeric reward (e.g. 75) by stringifying it."""
        return "" if v is None else str(v)

    @field_validator("timeline_order", mode="before")
    @classmethod
    def _coerce_timeline_order(cls, v: Any) -> Any:
        """Accept ints or numeric strings for a quest's place on the timeline."""
        if v in (None, ""):
            return None
        if isinstance(v, bool):
            return None
        if isinstance(v, int):
            return v
        if isinstance(v, str):
            s = v.strip()
            if s and s.lstrip("-").isdigit():
                return int(s)
        return v


def parse_quest(raw: str) -> Quest:
    """Parse a model's (possibly fenced) JSON response into a validated Quest.

    Shared by generation and LLM-backed repair so both tolerate ```json fences from
    chatty models and enforce the Quest schema as the single structural contract.
    """
    s = raw.strip()
    if s.startswith("```"):  # tolerate ```json fences
        s = s[s.find("{") : s.rfind("}") + 1]
    return Quest(**json.loads(s))


class MockQuestGenerator:
    """P0 deterministic generator. Emits ONE inconsistent reference ('Shadowfen') so the
    verify -> repair loop has something to catch. Kept for the P0 demo / regression test."""

    def __init__(self, gateway: LLMGateway):
        self.gateway = gateway

    def generate(self, intent: str) -> dict[str, Any]:
        self.gateway.complete(
            task="generate",
            system="You generate quests consistent with the World Bible.",
            user=intent,
        )
        return Quest(
            title="The Missing Caravan",
            giver_npc="Aldric",
            location="Shadowfen",
            objective="Track down the lost supply caravan on the northern road",
            reward="50 gold",
        ).model_dump(exclude_none=True)


class GroundedQuestGenerator:
    """P1: retrieval-grounded, structured generation.

    1. Retrieve the relevant lore sub-graph for the intent (token-efficient context).
    2. Ask the model (JSON / function-calling) for a Quest grounded ONLY on that context.
    3. Parse the structured response into a Quest.

    With a real model this passes verification on the first try most of the time; the
    consistency validators remain the safety net for the cases where it does not.

    `prefix_mode` controls the prompt's cache profile (P2 trade-off, quantified by the
    benchmark):
      - "retrieval" (default): put only the intent-relevant lore in the system prompt — short
        prompt, low single-call cost, but a different prefix per intent so the provider's
        server-side prefix cache fragments across tasks.
      - "stable": put the SAME fixed system rules + whole-world lore at the top of every call
        (nothing call-varying in the prefix) and let only the user/intent vary — bigger prompt
        and a pricier first miss, but that long prefix is then nearly free to reuse on the
        provider's prefix cache for batch generation over one world.
    """

    def __init__(
        self,
        gateway: LLMGateway,
        wb: WorldBible,
        *,
        prefix_mode: str = "retrieval",
        retrieval_top_k: int = 4,
    ):
        self.gateway = gateway
        self.wb = wb
        self.lore = LoreGraph(wb)
        self.prefix_mode = prefix_mode
        self.retrieval_top_k = retrieval_top_k
        self._stable_context: str | None = None

    def _retrieve(self, intent: str) -> str:
        """Return a compact textual excerpt of the lore relevant to the intent."""
        il = intent.lower()
        seeds = [e.id for e in self.wb.entities.values() if e.name.lower() in il]
        scored = self._rank_entities(intent)
        candidate_ids = seeds or [eid for eid, _score in scored[: self.retrieval_top_k]]

        if candidate_ids:
            ids = set(candidate_ids)
            for s in candidate_ids:
                ids.update(self.lore.neighbors(s, radius=1))
        else:
            ids = set(self.wb.entities.keys())  # small world: fall back to everything

        lines: list[str] = []
        for eid in sorted(ids):
            e = self.wb.entities[eid]
            tags = f" [{', '.join(e.tags)}]" if e.tags else ""
            lines.append(f"- {e.name} ({e.type.value}): {e.description}{tags}")
        for r in self.wb.relations:
            if r.source in ids and r.target in ids:
                src = self.wb.entities[r.source].name
                tgt = self.wb.entities[r.target].name
                lines.append(f"- {src} {r.kind} {tgt}")
        return "\n".join(lines)

    def _rank_entities(self, intent: str) -> list[tuple[str, float]]:
        """Lexical retriever over names/descriptions/tags/relations.

        Exact entity-name mentions remain the strongest signal, but this also handles prompts that
        say "healer", "hostile marsh", or "caravan route" without spelling the entity name.
        """
        query = _tokens(intent)
        if not query:
            return []

        relation_text: dict[str, list[str]] = {eid: [] for eid in self.wb.entities}
        for r in self.wb.relations:
            relation_text.setdefault(r.source, []).append(r.kind.replace("_", " "))
            relation_text.setdefault(r.target, []).append(r.kind.replace("_", " "))
            if r.target in self.wb.entities:
                relation_text.setdefault(r.source, []).append(self.wb.entities[r.target].name)
            if r.source in self.wb.entities:
                relation_text.setdefault(r.target, []).append(self.wb.entities[r.source].name)

        scored: list[tuple[str, float]] = []
        for eid, e in self.wb.entities.items():
            name_tokens = _tokens(e.name)
            desc_tokens = _tokens(e.description)
            tag_tokens = _tokens(" ".join(e.tags))
            rel_tokens = _tokens(" ".join(relation_text.get(eid, [])))
            score = 0.0
            if e.name.lower() in intent.lower():
                score += 8.0
            score += 4.0 * len(query & name_tokens)
            score += 2.0 * len(query & tag_tokens)
            score += 1.5 * len(query & rel_tokens)
            score += 1.0 * len(query & desc_tokens)
            if score > 0:
                scored.append((eid, score))
        return sorted(scored, key=lambda item: (-item[1], self.wb.entities[item[0]].name))

    def _context_for(self, intent: str) -> str:
        """Lore block for the prompt. 'stable' returns the whole world, computed once, so the
        system prefix is byte-identical on every call (provider-prefix-cache friendly)."""
        if self.prefix_mode == "stable":
            if self._stable_context is None:
                self._stable_context = self._retrieve("")  # no seeds -> whole world
            return self._stable_context
        return self._retrieve(intent)

    def generate(self, intent: str, *, tier: str | None = None) -> dict[str, Any]:
        context = self._context_for(intent)
        system = (
            "You write quests that are STRICTLY consistent with the World Bible below. "
            "Only reference NPCs and locations that appear in it; never invent new ones.\n"
            "Return ONE JSON object with keys: "
            "title, giver_npc, location, objective, reward, prerequisites, timeline_order.\n"
            "Use timeline_order when the quest has a clear place on the canonical timeline.\n\n"
            f"World Bible:\n{context}"
        )
        # Real models occasionally return an empty / non-JSON completion. Retry once (the
        # gateway doesn't cache empty responses, so the retry is a genuine fresh call) before
        # giving up with a clear error instead of a raw JSONDecodeError.
        last_err: Exception | None = None
        for _ in range(2):
            raw = self.gateway.complete(task="generate", system=system, user=intent, tier=tier)
            try:
                return parse_quest(raw).model_dump(exclude_none=True)
            except Exception as e:  # JSONDecodeError or pydantic ValidationError
                last_err = e
        raise ValueError(f"generation returned no parseable JSON after a retry: {last_err}")


class CascadingQuestGenerator:
    """T5 cascade generation: generate cheap, validate, escalate to strong only if lore breaks.

    Wraps a GroundedQuestGenerator. The first attempt routes through the gateway with no tier
    hint, so a CascadeRouter sends it to the cheap tier. We then run the SAME deterministic
    consistency validators the orchestrator uses; on any error-level issue we regenerate with
    an explicit strong-tier hint (one escalation). Both attempts go through the gateway
    (guardrail #1). `escalations` is exposed so the benchmark can report an escalation rate.

    The cheap tier handles the easy majority; if even the strong tier is inconsistent, the
    downstream verify -> repair loop is still the backstop — so cascading never lowers the
    first-pass consistency rate, it just makes the common case cheaper.
    """

    def __init__(
        self,
        base: GroundedQuestGenerator,
        validators: Sequence[Callable[[dict[str, Any]], list]],
        *,
        strong_tier: str = "frontier",
    ):
        self.base = base
        self.validators = list(validators)
        self.strong_tier = strong_tier
        self.escalations = 0

    def generate(self, intent: str) -> dict[str, Any]:
        artifact = self.base.generate(intent)  # cheap (CascadeRouter)
        if self._has_errors(artifact):
            artifact = self.base.generate(intent, tier=self.strong_tier)  # escalate once
            self.escalations += 1
        return artifact

    def _has_errors(self, artifact: dict[str, Any]) -> bool:
        return any(
            getattr(issue, "severity", "error") == "error"
            for v in self.validators
            for issue in v(artifact)
        )
