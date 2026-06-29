"""BPE tokenizer for context-budget accounting (approximate for non-OpenAI models).

Uses tiktoken cl100k_base (the vocabulary shared by GPT-3.5/4 and most OpenAI-compatible models).
The encoder is lazy-loaded and cached — the first call pays a small parse cost (< 10 ms);
subsequent calls are O(n) over the text only.

Accuracy / honesty note
------------------------
This is EXACT only for models that genuinely use the cl100k_base vocabulary (the OpenAI family).
For DeepSeek and other non-OpenAI OpenAI-compatible providers the count is an **approximation**:
those models use their own BPE vocabularies that DeepSeek does not publish as a local tokenizer,
so cl100k is the closest freely-available, offline, $0 stand-in. The error is **bounded and small**
in practice (both are byte-level BPE over largely overlapping merges; counts differ by a few
percent on mixed CJK+JSON text, not by integer multiples the way ``char÷4`` does), which is why it
is used here. It is used **only for context-budget / truncation decisions, never for billing or
cost reporting** — those read the provider's own ``usage`` token counts (see telemetry.CallRecord).
If a target model ever ships a real local tokenizer, prefer it over this approximation.

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

_ENCODING_NAME = "cl100k_base"  # DeepSeek V4-compatible vocabulary


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
    approximation** for non-OpenAI models such as DeepSeek (which use their own, unpublished BPE
    vocabulary — see the module docstring). When tiktoken itself is unavailable it falls back to
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


def reset_encoder_cache() -> None:
    """Reset the module-level encoder cache (test helper).

    Calling this forces the next :func:`count_tokens` call to re-attempt loading the
    encoder.  Useful when patching ``_ENCODER`` or ``tiktoken`` in unit tests.
    """
    global _ENCODER
    _ENCODER = None
