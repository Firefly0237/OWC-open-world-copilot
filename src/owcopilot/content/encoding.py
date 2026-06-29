"""Tolerant byte decoding for imported files.

Planners hand us files saved by whatever tool they use: a UTF-8 manuscript, a BOM-prefixed
export, or — very commonly on a Chinese Windows machine — a GB18030 CSV straight out of Excel.
Decoding everything as UTF-8 crashes on exactly those real files, so we try a small ladder of
encodings and only fall back to lossy replacement as a last resort (never a crash).

UTF-16 handling
---------------
Files with a UTF-16 BOM (LE: ``\\xff\\xfe``; BE: ``\\xfe\\xff``) are detected and decoded
directly — no guessing needed.

UTF-16 *without* a BOM is fundamentally ambiguous: every pure-ASCII character in UTF-16 LE
produces an interleaved ``\\x00`` byte, which ``utf-8`` silently accepts as a legal NUL
character.  This causes *silent data corruption* — the most dangerous failure mode.  When the
NUL-interleave heuristic fires we therefore raise ``ValueError`` rather than letting garbage
through silently, giving the user a clear message to re-save as UTF-8.
"""

from __future__ import annotations

# utf-8-sig strips a BOM if present; gb18030 is a superset of GBK/GB2312 (Chinese Excel/Windows);
# cp1252 covers western single-byte exports. Order matters: stricter/likelier first.
_DECODE_LADDER = ("utf-8-sig", "utf-8", "gb18030", "cp1252")

# BOM byte sequences and their corresponding encodings (checked before the ladder).
# For UTF-16 we use the "utf-16" codec (not "utf-16-le"/"utf-16-be") because the former
# auto-detects and strips the BOM character, while the latter codecs include it in output.
_BOM_MAP: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xfe\x00\x00", "utf-32-le"),  # must come before utf-16 LE (4-byte prefix)
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe", "utf-16"),   # utf-16 auto-strips BOM; covers both LE (0xff 0xfe)
    (b"\xfe\xff", "utf-16"),   # and BE (0xfe 0xff)
    (b"\xef\xbb\xbf", "utf-8-sig"),  # handled by ladder, listed here for completeness
)

# Threshold: if more than 40 % of even-indexed bytes are NUL, the data looks UTF-16 LE w/o BOM.
_UTF16_NUL_RATIO_THRESHOLD = 0.40


def _detect_bom(data: bytes) -> str | None:
    """Return the encoding indicated by a leading BOM, or None if no BOM is present."""
    for bom, encoding in _BOM_MAP:
        if data.startswith(bom):
            return encoding
    return None


def _looks_like_utf16_without_bom(data: bytes) -> bool:
    """Heuristic: alternating NUL bytes typical of UTF-16 LE plain-ASCII content w/o BOM.

    Only applied when the file has no BOM and the ladder would otherwise accept it silently.
    """
    if len(data) < 8:
        return False
    # Check the second byte of every 2-byte pair (odd indices for UTF-16 LE ASCII content)
    odd_bytes = data[1::2]
    nul_count = odd_bytes.count(b"\x00")
    return (nul_count / len(odd_bytes)) > _UTF16_NUL_RATIO_THRESHOLD


def decode_bytes(data: bytes) -> str:
    """Decode uploaded file bytes to text, trying common encodings before lossy replacement.

    Priority:
    1. BOM detection (UTF-16 LE/BE, UTF-32, UTF-8 BOM) — unambiguous, used directly.
    2. Encoding ladder: utf-8-sig → utf-8 → gb18030 → cp1252 — first that does not raise wins.
    3. UTF-16 without BOM heuristic — fires *after* the ladder to catch silent corruption.
    4. Lossy replacement (utf-8 errors=replace) — last resort, never crashes.
    """
    if not data:
        return ""

    # 1. BOM — unambiguous
    bom_encoding = _detect_bom(data)
    if bom_encoding:
        return data.decode(bom_encoding)

    # 2. Encoding ladder
    decoded: str | None = None
    used_encoding: str | None = None
    for encoding in _DECODE_LADDER:
        try:
            decoded = data.decode(encoding)
            used_encoding = encoding
            break
        except UnicodeDecodeError:
            continue

    if decoded is None:
        # All ladder encodings failed — lossy fallback
        return data.decode("utf-8", errors="replace")

    # 3. UTF-16 without BOM heuristic (only when utf-8 "succeeded" but may be corrupt)
    # gb18030 and cp1252 legitimately produce high-byte content — only warn for utf-8/utf-8-sig
    if used_encoding in {"utf-8", "utf-8-sig"} and _looks_like_utf16_without_bom(data):
        raise ValueError(
            "文件疑似 UTF-16 编码但缺少 BOM 标记。utf-8 解码虽不报错，但数据已损坏。"
            "请将文件重新以 UTF-8（带或不带 BOM 均可）或 UTF-16（带 BOM）格式保存后重试。"
        )

    return decoded
