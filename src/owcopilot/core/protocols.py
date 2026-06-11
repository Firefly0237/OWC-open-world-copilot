"""Engine-agnostic boundaries. Concrete impls live in adapters/, generation/, consistency/.

Keeping these as Protocols means the orchestrator depends on *behaviour*, not on any
specific engine or model — which is exactly what lets the same core drive Unity and Unreal.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .state import ValidationIssue


@runtime_checkable
class EngineAdapter(Protocol):
    """Pluggable engine boundary. Unreal/Unity adapters implement this."""

    name: str

    def apply(self, artifact: dict[str, Any]) -> None:
        """Land a generated artifact into the engine (DataTable / Blueprint / Level / ...)."""
        ...

    def snapshot(self) -> dict[str, Any]:
        """Return a serialisable view of what has been applied (for verification/preview)."""
        ...


@runtime_checkable
class Generator(Protocol):
    """Turns an intent into a structured artifact (ideally via function-calling)."""

    def generate(self, intent: str) -> dict[str, Any]: ...


@runtime_checkable
class Validator(Protocol):
    """A deterministic-or-LLM consistency check. Returns the issues it found (possibly empty)."""

    def __call__(self, artifact: dict[str, Any]) -> list[ValidationIssue]: ...
