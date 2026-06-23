"""Offline test doubles, collected in one place.

``MockProvider`` is the deterministic, dependency-free stand-in for a model: it echoes the prompt
so tests can exercise the gateway (routing, caching, telemetry) offline at $0. It is kept out of
``llm/gateway.py`` (real implementations only) and re-exported from there for compatibility.

Note: ``HashingEmbedder`` is **not** a test double. It is the real default ``SemanticCache``
embedder, so it lives in ``llm/cache.py``. Feature-specific offline providers (QA, drafts,
world-seed, …) live next to their feature in that package's ``offline.py``.
"""

from __future__ import annotations


class MockProvider:
    """Deterministic, offline provider for tests. Token counts approximated as len/4."""

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        text = f"[mock:{model}] " + (user[:60] if user else "")
        in_tok = max(1, (len(system) + len(user)) // 4)
        out_tok = max(1, len(text) // 4)
        return text, in_tok, out_tok


__all__ = ["MockProvider"]
