# 上下文预算记账用的 BPE tokenizer（非 OpenAI 模型为近似）。
"""BPE tokenizer for context-budget accounting (approximate for non-OpenAI models).

Uses tiktoken cl100k_base (the vocabulary shared by GPT-3.5/4 and most OpenAI-compatible models).
The encoder is lazy-loaded and cached — the first call pays a small parse cost (< 10 ms);
subsequent calls are O(n) over the text only.

Accuracy / honesty note
------------------------
This is EXACT only for models that genuinely use the cl100k_base vocabulary (the OpenAI family).
For DeepSeek and other non-OpenAI providers the count is an **approximation**: those models use
their own BPE vocabularies. DeepSeek *does* publish an official offline tokenizer — the
``deepseek_v3_tokenizer`` (HuggingFace format; download
https://cdn.deepseek.com/api-docs/deepseek_v3_tokenizer.zip, and ``tokenizer.json`` /
``tokenizer_config.json`` in the HF repo ``deepseek-ai/DeepSeek-V3``) — so a real local DeepSeek
tokenizer exists and is distinct from cl100k. We deliberately keep cl100k_base here as an offline,
$0 **approximation** used ONLY for context-budget / truncation decisions (never for billing), to
avoid taking a ``transformers`` / model-download dependency; we do **not** integrate DeepSeek's
tokenizer (that would be a separate, opt-in choice). The error is **bounded and small** in practice
(both are byte-level BPE over largely overlapping merges; counts differ by a few percent on mixed
CJK+JSON text, not by the integer multiples the way ``char÷4`` does), which is why the approximation
is acceptable here. Cost/usage reporting always reads the provider's own ``usage`` token counts
(see telemetry.CallRecord), so this approximation can never distort a charge. If we ever take the
model-download dependency, prefer DeepSeek's own tokenizer over this approximation.

Why tiktoken and not char÷4
----------------------------
- English text: ~4 chars/token → char÷4 is close.
- Chinese/Japanese/Korean: ~1 char/token → char÷4 over-estimates tokens by ~4×, causing
  over-eager transcript truncation for CJK-heavy content.
- Mixed CJK + JSON (typical for this project): no single char/token ratio is correct.
- tiktoken uses Rust-backed BPE, runs locally, has no network calls, and costs $0.

Install
-------
  pip install tiktoken           # standalone (≈1 MB, no torch)
  pip install owcopilot[tokenizer]  # via pyproject.toml extra

Fallback
--------
When tiktoken is not installed the module falls back to ``len(text) // 4`` (same as the
previous char-based estimate) but emits a WARNING so the degradation is never silent.
The fallback path can be exercised deterministically in tests by patching _ENCODER to
``_FALLBACK_SENTINEL``.
"""

from __future__ import annotations

import logging
import warnings

_log = logging.getLogger(__name__)

# Sentinel used internally to force the fallback path (e.g. in tests that verify the
# no-tiktoken warning).
_FALLBACK_SENTINEL = object()

# Module-level encoder cache.  None = not yet initialised.  _FALLBACK_SENTINEL = tiktoken
# is unavailable and we already emitted the warning.  Otherwise an actual tiktoken Encoding.
_ENCODER: object = None  # tiktoken.Encoding | _FALLBACK_SENTINEL | None

_ENCODING_NAME = "cl100k_base"  # offline $0 approximation of the DeepSeek vocab (see docstring)


def _load_encoder() -> object:
    """Attempt to load the tiktoken encoder, returning it or _FALLBACK_SENTINEL."""
    global _ENCODER
    if _ENCODER is not None:
        return _ENCODER

    try:
        import tiktoken  # noqa: PLC0415 — intentional lazy import

        enc = tiktoken.get_encoding(_ENCODING_NAME)
        _ENCODER = enc
        _log.debug("tiktoken %s encoder loaded (cl100k_base).", _ENCODING_NAME)
        return _ENCODER
    except ImportError:
        warnings.warn(
            "tiktoken is not installed — token counting falls back to len(text)//4, "
            "which is inaccurate for CJK-heavy content. "
            "Install it with:  pip install tiktoken  (or pip install owcopilot[tokenizer])",
            UserWarning,
            stacklevel=3,
        )
        _ENCODER = _FALLBACK_SENTINEL
        return _ENCODER
    except Exception as exc:  # encoding file corrupt / env issue
        warnings.warn(
            f"tiktoken failed to load ({exc!r}) — falling back to len(text)//4. "
            "Re-install tiktoken to restore accurate token counting.",
            UserWarning,
            stacklevel=3,
        )
        _ENCODER = _FALLBACK_SENTINEL
        return _ENCODER


def count_tokens(text: str) -> int:
    """Count BPE tokens in *text* using tiktoken cl100k_base.

    Returns an **exact** count for cl100k-based (OpenAI-family) models, or a **bounded
    approximation** for non-OpenAI models such as DeepSeek (which use their own BPE vocabulary; a
    real local DeepSeek tokenizer exists but is deliberately not integrated — see the module
    docstring). When tiktoken itself is unavailable it falls back to
    ``max(1, len(text) // 4)`` (never returns 0 for non-empty text).

    This count is for **context-budget / truncation decisions only — never for billing**. Cost
    reporting reads the provider's own ``usage`` token counts, so the approximation here can never
    distort a charge. It is the canonical token-counting primitive for the project's context-budget
    calculations and must never crash: all exceptions from tiktoken are caught and fall through to
    the documented fallback.
    """
    if not text:
        return 0

    enc = _load_encoder()

    if enc is _FALLBACK_SENTINEL:
        return max(1, len(text) // 4)

    try:
        # enc is a tiktoken.Encoding at this point; we avoid importing tiktoken at module level
        # so mypy can't verify the attribute — use a cast via Any to avoid the attr-defined error.
        import tiktoken as _tiktoken  # noqa: PLC0415

        assert isinstance(enc, _tiktoken.Encoding)
        token_ids: list[int] = enc.encode(text)
        return len(token_ids)
    except Exception as exc:
        _log.warning("tiktoken.encode() failed (%r); using fallback for this call.", exc)
        return max(1, len(text) // 4)


def truncate_to_token_budget(text: str, max_tokens: int) -> tuple[str, int]:
    """Cut *text* down to at most *max_tokens* tokens, returning ``(head, kept_tokens)``.

    This is the canonical tiktoken truncation recipe (OpenAI Cookbook,
    ``truncate_text_tokens`` in "Embedding long inputs"): encode the string, slice the token id
    stream to the budget, and decode that prefix back to text —
    ``encoding.decode(encoding.encode(text)[:max_tokens])``. Decoding a *prefix* of the full token
    stream is safe: ``decode(encode(text)) == text`` round-trips, and the only lossy case is a
    single final token straddling a UTF-8 boundary, which yields a replacement char at the very
    tail — acceptable for a truncation head (see "How to count tokens with tiktoken"). Cutting by
    TOKENS (not chars) means the budget is language-independent: 1000 tokens is 1000 tokens whether
    the text is English or CJK.

    - ``max_tokens <= 0`` or empty text → ``("", 0)``.
    - text already within budget → ``(text, actual_token_count)`` (no re-encode round-trip applied).
    - over budget → ``(decode(ids[:max_tokens]), max_tokens)``.

    Fallback (tiktoken unavailable, or any encode/decode failure): a char-proportional head
    ``text[:max_tokens * 4]`` with ``kept = max(1, len(head) // 4)`` — the same ``÷4`` model as
    :func:`count_tokens`' fallback, so the two stay consistent. Never crashes.
    """
    if max_tokens <= 0 or not text:
        return "", 0

    enc = _load_encoder()

    if enc is _FALLBACK_SENTINEL:
        # _load_encoder already emitted the no-tiktoken warning when it set the sentinel, so the
        # degradation is never silent — mirror count_tokens' fallback here without re-warning.
        head = text[: max_tokens * 4]
        return head, max(1, len(head) // 4)

    try:
        import tiktoken as _tiktoken  # noqa: PLC0415 — intentional lazy import

        assert isinstance(enc, _tiktoken.Encoding)
        ids: list[int] = enc.encode(text)
        if len(ids) <= max_tokens:
            return text, len(ids)
        kept = ids[:max_tokens]
        return enc.decode(kept), len(kept)
    except Exception as exc:
        _log.warning(
            "tiktoken truncate failed (%r); using char-proportional fallback for this call.", exc
        )
        head = text[: max_tokens * 4]
        return head, max(1, len(head) // 4)


def reset_encoder_cache() -> None:
    """Reset the module-level encoder cache (test helper).

    Calling this forces the next :func:`count_tokens` call to re-attempt loading the
    encoder.  Useful when patching ``_ENCODER`` or ``tiktoken`` in unit tests.
    """
    global _ENCODER
    _ENCODER = None
