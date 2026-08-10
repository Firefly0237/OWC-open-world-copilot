"""The offline/fake LLM providers are a TEST & CI fixture, never a shipped product mode.

A real deployment (no `OWCOPILOT_ALLOW_OFFLINE_LLM` opt-in) must fail closed for any AI feature
instead of serving canned output as if it were the model's. The whole suite enables the opt-in via
the autouse conftest fixture; these tests deliberately remove it to assert the production contract.
"""

from __future__ import annotations

import pytest

from owcopilot.llm.gateway import (
    OFFLINE_LLM_FORBIDDEN_MESSAGE,
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


def test_api_ask_fails_closed_without_optin(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from owcopilot.service.api import create_app

    monkeypatch.delenv("OWCOPILOT_ALLOW_OFFLINE_LLM", raising=False)
    client = TestClient(create_app(), raise_server_exceptions=False)
    response = client.post(
        "/projects/demo/ask",
        json={
            "content": {
                "entities": {
                    "npc_aldric": {
                        "id": "npc_aldric",
                        "name": "Aldric",
                        "type": "npc",
                        "description": "Caravan master.",
                    }
                }
            },
            "query": "Who is Aldric?",
            "llm_mode": "offline",
        },
    )
    assert response.status_code == 503
    assert response.json()["detail"] == OFFLINE_LLM_FORBIDDEN_MESSAGE
