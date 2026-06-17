"""Tolerant byte decoding for imported files.

Planners hand us files saved by whatever tool they use: a UTF-8 manuscript, a BOM-prefixed
export, or — very commonly on a Chinese Windows machine — a GB18030 CSV straight out of Excel.
Decoding everything as UTF-8 crashes on exactly those real files, so we try a small ladder of
encodings and only fall back to lossy replacement as a last resort (never a crash).
"""

from __future__ import annotations

# utf-8-sig strips a BOM if present; gb18030 is a superset of GBK/GB2312 (Chinese Excel/Windows);
# cp1252 covers western single-byte exports. Order matters: stricter/likelier first.
_DECODE_LADDER = ("utf-8-sig", "utf-8", "gb18030", "cp1252")


def decode_bytes(data: bytes) -> str:
    """Decode uploaded file bytes to text, trying common encodings before lossy replacement."""
    for encoding in _DECODE_LADDER:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")
