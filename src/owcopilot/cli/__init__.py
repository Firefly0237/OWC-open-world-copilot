"""Command-line interface package."""

from collections.abc import Sequence
from typing import Any

__all__ = ["main"]


def __getattr__(name: str) -> Any:
    if name == "main":
        from .main import main

        return main
    raise AttributeError(name)


def main(argv: Sequence[str] | None = None) -> int:
    from .main import main as _main

    return _main(argv)
