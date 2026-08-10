# 不可信标识符与路径段的统一校验边界。
"""Validation for untrusted identifiers that become filesystem path segments.

The important contract is deliberately small: validate, never rewrite.  Callers may use CJK and
ordinary internal spaces, but a value that could be interpreted as a path (including an encoded
path) is rejected rather than silently normalised into a different identifier.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path, PurePosixPath, PureWindowsPath

from .security import PathSecurityError, resolve_under_root

_PATH_HOSTILE_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_PERCENT_ESCAPE_RE = re.compile(r"%[0-9a-fA-F]{2}")
_RESERVED_WINDOWS_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def _looks_like_path(value: str) -> bool:
    """Whether ``value`` carries path syntax on either supported filesystem family."""
    return bool(
        _PATH_HOSTILE_RE.search(value)
        or _PERCENT_ESCAPE_RE.search(value)
        or ".." in value
        or value in {".", ".."}
        or PurePosixPath(value).is_absolute()
        or PureWindowsPath(value).is_absolute()
        or bool(PureWindowsPath(value).drive)
        or Path(value).is_absolute()
    )


def validate_path_segment(
    value: str,
    *,
    label: str = "标识符",
    max_length: int = 64,
) -> str:
    """Return one safe path segment unchanged, or raise :class:`ValueError`.

    Checks run over both the original value and its NFKC form.  This catches compatibility forms
    such as a full-width slash without changing a legal CJK name.  Percent escape triplets are
    rejected outright, including the ``%25`` prefix used by double-encoded traversal payloads.
    """
    if not isinstance(value, str):
        raise ValueError(f"非法的{label}：必须是字符串")
    if not value or len(value) > max_length:
        raise ValueError(f"非法的{label}：长度必须为 1..{max_length}")
    if any(unicodedata.category(char) in {"Cc", "Cs"} for char in value):
        raise ValueError(f"非法的{label}：不能含控制字符或无效代理字符")
    if value != value.strip() or value.rstrip(". ") != value:
        raise ValueError(f"非法的{label}：不能含首尾空白或句点")

    compatibility_form = unicodedata.normalize("NFKC", value)
    if _looks_like_path(value) or _looks_like_path(compatibility_form):
        raise ValueError(f"非法的{label}：{value!r} 不是单一路径段")

    # Windows reserves device names even with an extension (CON.txt, LPT1.json, ...).
    if compatibility_form.split(".", 1)[0].upper() in _RESERVED_WINDOWS_NAMES:
        raise ValueError(f"非法的{label}：{value!r} 是系统保留名称")
    return value


def validate_tenant_id(value: str) -> str:
    """Tenant ids share the same path-segment invariant at every construction boundary."""
    return validate_path_segment(value, label="tenant_id")


def resolve_path_segment(
    root: str | Path,
    value: str,
    *,
    label: str = "标识符",
    max_length: int = 64,
) -> Path:
    """Validate ``value`` and resolve it as a direct, non-symlink child of ``root``.

    ``resolve_under_root`` supplies the containment assertion.  The equality/direct-parent checks
    additionally reject an existing symlink or junction that aliases another directory, even when
    that target happens to remain under ``root``.
    """
    segment = validate_path_segment(value, label=label, max_length=max_length)
    resolved_root = Path(root).expanduser().resolve()
    lexical_target = resolved_root / segment
    resolved_target = resolve_under_root(resolved_root, lexical_target)
    if resolved_target.parent != resolved_root or resolved_target != lexical_target:
        raise PathSecurityError(
            f"path segment {value!r} does not resolve to a direct child of {resolved_root}"
        )
    return resolved_target


__all__ = ["resolve_path_segment", "validate_path_segment", "validate_tenant_id"]
