"""Shared test fixtures.

The whole suite must stay deterministic, offline and $0 regardless of what is installed in
the dev environment. The retrieval stack defaults to the real semantic embedder (bge-m3) when
the optional ``[semantic]`` extra is present, so we pin every test to the deterministic
``HashingEmbedder`` here. The real semantic path has its own dedicated, skip-if-unavailable
test (``test_retrieval_semantic.py``) instead of slowing the core suite with a 2GB model.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _pin_hashing_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OWCOPILOT_EMBEDDER", "hashing")
    # The offline/fake LLM providers are a test & CI fixture, not a shipped product mode: the
    # runtime builders fail closed unless this opt-in is set. The suite drives those builders in
    # offline mode, so enable the fixture for every test (a real deployment never sets it).
    monkeypatch.setenv("OWCOPILOT_ALLOW_OFFLINE_LLM", "1")
