"""JSON Pointer parsing shared by the shadow-apply and rollback patch primitives."""

from __future__ import annotations


def pointer_parts(path: str) -> list[str]:
    """Split a JSON Pointer into its decoded reference tokens (``~1``→``/``, ``~0``→``~``)."""
    if not path.startswith("/"):
        raise ValueError(f"patch path must be a JSON pointer: {path!r}")
    return [part.replace("~1", "/").replace("~0", "~") for part in path.split("/")[1:]]
