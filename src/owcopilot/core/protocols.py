"""Engine-agnostic boundaries. Concrete impls live in generation/, consistency/.

Keeping these as Protocols means the orchestrator depends on *behaviour*, not on any
specific model — the generator and validators are injected.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .state import ValidationIssue


@runtime_checkable
class Generator(Protocol):
    """Turns an intent into a structured artifact (ideally via function-calling)."""

    def generate(self, intent: str) -> dict[str, Any]: ...


@runtime_checkable
class Validator(Protocol):
    """A deterministic-or-LLM consistency check. Returns the issues it found (possibly empty)."""

    def __call__(self, artifact: dict[str, Any]) -> list[ValidationIssue]: ...
