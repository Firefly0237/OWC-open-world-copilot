"""Filesystem path safety helpers."""

from __future__ import annotations

from pathlib import Path


class PathSecurityError(ValueError):
    """Raised when a requested path escapes an allowed root."""


def resolve_under_root(root: str | Path, candidate: str | Path) -> Path:
    """Resolve `candidate` and require it to stay within `root`.

    Relative candidates are interpreted relative to `root`; absolute candidates are accepted only
    if their resolved path is still inside the resolved root.
    """
    resolved_root = Path(root).expanduser().resolve()
    candidate_path = Path(candidate).expanduser()
    resolved = (
        candidate_path.resolve()
        if candidate_path.is_absolute()
        else (resolved_root / candidate_path).resolve()
    )
    try:
        resolved.relative_to(resolved_root)
    except ValueError as e:
        raise PathSecurityError(f"path {resolved} escapes allowed root {resolved_root}") from e
    return resolved
