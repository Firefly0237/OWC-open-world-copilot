"""Tier routing for the gateway.

P0 ships a StaticRouter (task name -> tier). P2 replaces it with a CascadeRouter that
tries a cheap model first and escalates to frontier only on low confidence
(FrugalGPT / RouteLLM style).
"""

from __future__ import annotations

from typing import Protocol


class Router(Protocol):
    def choose(self, *, task: str, hint: str | None = None) -> str: ...


class StaticRouter:
    """Map a task name to a model tier. Cheap by default; explicit hint always wins."""

    DEFAULT_MAP = {
        "plan": "cheap",
        "retrieve": "cheap",
        "validate": "cheap",
        "generate": "frontier",
        "repair": "frontier",
    }

    def __init__(self, mapping: dict[str, str] | None = None, default_tier: str = "cheap"):
        self.mapping = dict(mapping or self.DEFAULT_MAP)
        self.default_tier = default_tier

    def choose(self, *, task: str, hint: str | None = None) -> str:
        return hint or self.mapping.get(task, self.default_tier)


class CascadeRouter:
    """Route the cascade tasks to the CHEAP tier first; let the caller escalate on demand.

    A router can only see the task name, never the output — so it can't itself decide "this
    answer is bad, retry stronger". It does the half it *can*: for `cascade_tasks` (default:
    `generate`) it picks the cheap tier by default, so generation starts cheap. The other
    half — run the consistency validators on that cheap output and, if they fail, re-issue the
    call with an explicit `tier=strong` hint — lives in `CascadingQuestGenerator`
    (generation/quest.py). An explicit hint always wins, which is exactly how that wrapper
    forces the escalation. Non-cascade tasks (plan/validate/repair) defer to `base`.

    This reuses P1's deterministic validators as the cascade's confidence signal: no extra
    scoring model, and "cheap unless it breaks lore" falls straight out.
    """

    def __init__(
        self,
        *,
        cheap: str = "cheap",
        strong: str = "frontier",
        cascade_tasks: tuple[str, ...] = ("generate",),
        base: Router | None = None,
    ):
        self.cheap = cheap
        self.strong = strong
        self.cascade_tasks = set(cascade_tasks)
        self.base = base or StaticRouter()

    def choose(self, *, task: str, hint: str | None = None) -> str:
        if hint:
            return hint  # explicit escalation (or any caller hint) wins
        if task in self.cascade_tasks:
            return self.cheap  # start cheap; the wrapper escalates if lore breaks
        return self.base.choose(task=task)
