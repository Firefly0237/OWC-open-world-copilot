"""The offline/fake LLM providers are a TEST & CI fixture, never a shipped product mode.

A real deployment (no `OWCOPILOT_ALLOW_OFFLINE_LLM` opt-in) must fail closed for any AI feature
instead of serving canned output as if it were the model's. The whole suite enables the opt-in via
the autouse conftest fixture; these tests deliberately remove it to assert the production contract.
"""

from __future__ import annotations

import pytest

from owcopilot.llm.gateway import (
    offline_llm_allowed,
    require_offline_llm_allowed,
)


def test_offline_llm_forbidden_without_optin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OWCOPILOT_ALLOW_OFFLINE_LLM", raising=False)
    assert offline_llm_allowed() is False
    with pytest.raises(RuntimeError, match="接入模型"):
        require_offline_llm_allowed()


def test_offline_llm_allowed_with_optin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OWCOPILOT_ALLOW_OFFLINE_LLM", "1")
    assert offline_llm_allowed() is True
    require_offline_llm_allowed()  # does not raise
