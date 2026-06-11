"""Base class for engine adapters. Unreal (P3) and Unity (generality proof) subclass this."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseEngineAdapter(ABC):
    name: str = "base"

    @abstractmethod
    def apply(self, artifact: dict[str, Any]) -> None: ...

    @abstractmethod
    def snapshot(self) -> dict[str, Any]: ...
