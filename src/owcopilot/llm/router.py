"""Tier routing for the gateway: map a task name to a model tier.

The gateway calls ``router.choose(task=...)`` for every completion. An explicit per-call ``hint``
(tier) always wins; otherwise the task is looked up in the caller-supplied ``{task: tier}`` mapping,
falling back to ``default_tier`` for unmapped tasks.
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
