"""Controlled-provider checks of retry ownership; these do not claim live-model integration."""

from __future__ import annotations

import json

import pytest

from owcopilot.assist.drafts import QuestDraftService
from owcopilot.audit.default_rules import build_default_rule_registry
from owcopilot.audit.runner import AuditRunner
from owcopilot.content.models import ContentBundle, Quest, QuestStage
from owcopilot.llm.chat_model import ChatModelProvider
from owcopilot.llm.execution_policy import SINGLE_DISPATCH_POLICY
from owcopilot.llm.gateway import LLMGateway, LLMGatewayError
from owcopilot.llm.resilience import build_real_provider
from owcopilot.logic.draft import draft_quest_logic
from owcopilot.retrieval.bm25 import BM25Retriever
from owcopilot.retrieval.context_pack import ContextPackBuilder
from owcopilot.storage import SQLiteStore


class ReplyProvider:
    def __init__(self, reply: str | Exception) -> None:
        self.reply = reply
        self.calls = 0

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        self.calls += 1
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply, 1, 1


class UnexpectedCritic:
    def critique(self, **kwargs):
        raise AssertionError("single-dispatch generation must not invoke a critic")


def test_provider_policy_disables_sdk_retry_and_environment_failover(monkeypatch) -> None:
    monkeypatch.setenv("OWCOPILOT_FALLBACK_MODEL", "secondary")
    monkeypatch.delenv("OWCOPILOT_CIRCUIT_BREAKER", raising=False)
    provider = build_real_provider("primary", execution_policy=SINGLE_DISPATCH_POLICY)
    assert isinstance(provider, ChatModelProvider)
    assert provider.max_retries == 0
    assert provider.max_transport_attempts == 1


def test_gateway_policy_prevents_replay_of_transport_failure() -> None:
    provider = ReplyProvider(TimeoutError("controlled transport failure"))
    gateway = LLMGateway(
        {"cheap": provider}, max_retries=8, execution_policy=SINGLE_DISPATCH_POLICY
    )
    with pytest.raises(LLMGatewayError):
        gateway.complete(task="draft", system="Return JSON", user="test")
    assert provider.calls == 1


@pytest.mark.parametrize("valid", [False, True])
def test_quest_policy_disables_json_repair_and_critic(tmp_path, valid) -> None:
    provider = ReplyProvider(
        json.dumps({"title": "Harbour", "objective": "Find the medicine"}) if valid else "bad JSON"
    )
    gateway = LLMGateway({"cheap": provider}, execution_policy=SINGLE_DISPATCH_POLICY)
    store = SQLiteStore(tmp_path / "quest.sqlite3")
    try:
        service = QuestDraftService(
            gateway=gateway,
            context_builder=ContextPackBuilder(bm25=BM25Retriever(store)),
            audit_runner=AuditRunner(build_default_rule_registry()),
            bundle=ContentBundle(),
            critic=UnexpectedCritic(),
            max_refine_rounds=4,
            execution_policy=SINGLE_DISPATCH_POLICY,
        )
        if valid:
            result = service.draft_quest("Create a harbour quest")
            assert result.quest.title == "Harbour"
            assert result.refine_trail == []
        else:
            with pytest.raises(ValueError):
                service.draft_quest("Create a harbour quest")
    finally:
        store.close()
    assert provider.calls == 1


@pytest.mark.parametrize("valid", [False, True])
def test_logic_policy_returns_one_attempt_without_repair_or_refine(valid) -> None:
    provider = ReplyProvider(
        json.dumps(
            {
                "branches": [
                    {"id": "b1", "from_stage": "s1", "to_stage": "s2", "condition": "unknown"}
                ]
            }
        )
        if valid
        else '{"variables": "wrong type"}'
    )
    gateway = LLMGateway({"cheap": provider}, execution_policy=SINGLE_DISPATCH_POLICY)
    quest = Quest(
        id="q_harbour",
        title="Harbour",
        stages=[QuestStage(id="s1", summary="Start"), QuestStage(id="s2", summary="End")],
    )
    if valid:
        result = draft_quest_logic(
            gateway=gateway, quest=quest, max_rounds=4, execution_policy=SINGLE_DISPATCH_POLICY
        )
        assert result.issues  # Existing deterministic audit still reports the undefined variable.
        assert result.trail == []
        assert result.auto_review_incomplete
    else:
        with pytest.raises(ValueError, match="未能生成可解析"):
            draft_quest_logic(gateway=gateway, quest=quest, execution_policy=SINGLE_DISPATCH_POLICY)
    assert provider.calls == 1
