"""Tests for IN-2: cross-model evaluator gateway support.

Covers:
- evaluator_gateway=None: byte-level identical behavior to before
- evaluator_gateway routed: spy verifies evaluator is called, not main gateway
- evaluator_gateway failure: fallback to main gateway, result not None, summary marked
- QuestCritic and WorldQuestCritic both support evaluator_gateway
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from owcopilot.assist.critic import QuestCritic, critique_with_retry
from owcopilot.content.models import Quest
from owcopilot.llm.cache import NoOpCache
from owcopilot.llm.gateway import LLMGateway, LLMGatewayError
from owcopilot.llm.router import StaticRouter
from owcopilot.worldgen.critic import WorldQuestCritic

# ---------------------------------------------------------------------------
# Minimal offline providers for testing
# ---------------------------------------------------------------------------

_VALID_CRITIQUE = json.dumps({
    "verdict": "pass",
    "score": 0.85,
    "summary": "looks good",
    "dimensions": [{"dimension": "craft", "severity": "ok", "issue": "", "fix": ""}],
})

_VALID_REVISE_CRITIQUE = json.dumps({
    "verdict": "revise",
    "score": 0.4,
    "summary": "needs work",
    "dimensions": [{"dimension": "intent", "severity": "blocker", "issue": "bad", "fix": "fix it"}],
})


class _FixedProvider:
    """Returns a fixed response and records calls."""

    def __init__(self, response: str, name: str = "fixed") -> None:
        self.response = response
        self.calls: list[dict] = []
        self.name = name

    def complete(self, *, system: str, user: str, model: str) -> tuple:
        self.calls.append({"system": system, "user": user, "model": model})
        return self.response, 10, 5


class _ErrorProvider:
    """Always raises a provider-level error (gateway wraps to LLMGatewayError)."""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, *, system: str, user: str, model: str) -> tuple:
        self.calls += 1
        raise RuntimeError("evaluator is down")


def _make_gateway(provider: Any, name: str = "cheap") -> LLMGateway:
    return LLMGateway(
        providers={name: provider},
        router=StaticRouter(mapping={"quest_critique": name, "world_seed": name}),
        cache=NoOpCache(),
    )


# ---------------------------------------------------------------------------
# critique_with_retry: backward compatibility
# ---------------------------------------------------------------------------

def test_critique_with_retry_no_evaluator_uses_main_gateway() -> None:
    """[硬] evaluator_gateway=None: main gateway is used, behavior unchanged."""
    provider = _FixedProvider(_VALID_CRITIQUE, "main")
    gateway = _make_gateway(provider)
    result = critique_with_retry(
        gateway,
        task="quest_critique",
        system="STRICT_QUEST_REVIEWER",
        user="draft",
        evaluator_gateway=None,
    )
    assert result.verdict == "pass"
    assert len(provider.calls) >= 1


def test_critique_with_retry_evaluator_gateway_used() -> None:
    """[硬] When evaluator_gateway provided, evaluator is called (not main gateway)."""
    main_provider = _FixedProvider(_VALID_REVISE_CRITIQUE, "main")
    eval_provider = _FixedProvider(_VALID_CRITIQUE, "evaluator")
    main_gw = _make_gateway(main_provider)
    eval_gw = _make_gateway(eval_provider, "cheap")

    result = critique_with_retry(
        main_gw,
        task="quest_critique",
        system="STRICT_QUEST_REVIEWER",
        user="draft",
        evaluator_gateway=eval_gw,
    )
    # evaluator gives "pass"
    assert result.verdict == "pass"
    # main gateway was NOT called
    assert len(main_provider.calls) == 0
    # evaluator was called
    assert len(eval_provider.calls) >= 1


def test_critique_with_retry_evaluator_fallback_on_error() -> None:
    """[硬] evaluator_gateway raises LLMGatewayError -> fallback to main, result not None."""
    error_provider = _ErrorProvider()
    main_provider = _FixedProvider(_VALID_CRITIQUE, "main")
    main_gw = _make_gateway(main_provider)
    eval_gw = _make_gateway(error_provider)

    result = critique_with_retry(
        main_gw,
        task="quest_critique",
        system="STRICT_QUEST_REVIEWER",
        user="draft",
        evaluator_gateway=eval_gw,
    )
    # Should succeed via fallback
    assert result is not None
    assert result.verdict == "pass"
    # Fallback marker in summary
    assert "[evaluator-fallback]" in result.summary


def test_critique_with_retry_fallback_marker_present() -> None:
    """[软] Fallback result summary contains 'evaluator-fallback' marker."""
    error_provider = _ErrorProvider()
    main_provider = _FixedProvider(_VALID_CRITIQUE, "main")
    main_gw = _make_gateway(main_provider)
    eval_gw = _make_gateway(error_provider)

    result = critique_with_retry(
        main_gw,
        task="quest_critique",
        system="STRICT_QUEST_REVIEWER",
        user="draft",
        evaluator_gateway=eval_gw,
    )
    assert "[evaluator-fallback]" in result.summary


def test_no_evaluator_no_exception_propagated_on_main_error() -> None:
    """When evaluator_gateway is None and main gateway errors, exception propagates."""
    error_provider = _ErrorProvider()
    gw = _make_gateway(error_provider)
    with pytest.raises(LLMGatewayError):
        critique_with_retry(
            gw,
            task="quest_critique",
            system="STRICT_QUEST_REVIEWER",
            user="draft",
            evaluator_gateway=None,
        )


# ---------------------------------------------------------------------------
# QuestCritic.critique() with evaluator_gateway
# ---------------------------------------------------------------------------

def test_quest_critic_evaluator_gateway_parameter_accepted() -> None:
    """QuestCritic.critique() accepts evaluator_gateway without error."""
    main_provider = _FixedProvider(_VALID_CRITIQUE, "main")
    eval_provider = _FixedProvider(_VALID_CRITIQUE, "eval")
    main_gw = _make_gateway(main_provider)
    eval_gw = _make_gateway(eval_provider)

    critic = QuestCritic(gateway=main_gw)
    quest = Quest(id="q1", title="Test", objective="Do something")
    result = critic.critique(
        brief="test brief",
        quest=quest,
        context_lines=[],
        readiness_missing=[],
        evaluator_gateway=eval_gw,
    )
    assert result.verdict == "pass"
    # Main gateway was NOT called (evaluator was used)
    assert len(main_provider.calls) == 0
    assert len(eval_provider.calls) >= 1


def test_quest_critic_no_evaluator_backward_compatible() -> None:
    """[硬] QuestCritic without evaluator_gateway: behavior unchanged."""
    main_provider = _FixedProvider(_VALID_CRITIQUE, "main")
    main_gw = _make_gateway(main_provider)
    critic = QuestCritic(gateway=main_gw)
    quest = Quest(id="q1", title="Test", objective="Do something")
    result = critic.critique(
        brief="test brief",
        quest=quest,
        context_lines=[],
        readiness_missing=[],
    )
    assert result.verdict == "pass"
    assert len(main_provider.calls) >= 1


# ---------------------------------------------------------------------------
# WorldQuestCritic.critique() with evaluator_gateway
# ---------------------------------------------------------------------------

def test_world_quest_critic_evaluator_gateway_routed() -> None:
    """WorldQuestCritic uses evaluator_gateway when provided."""
    main_provider = _FixedProvider(_VALID_REVISE_CRITIQUE, "main")
    eval_provider = _FixedProvider(_VALID_CRITIQUE, "eval")
    main_gw = _make_gateway(main_provider)
    eval_gw = _make_gateway(eval_provider)

    critic = WorldQuestCritic(gateway=main_gw)
    result = critic.critique(
        brief="world brief",
        quests=[
            {"title": "q", "objective": "o", "stages": [{}, {}], "giver_npc": "g", "location": "l"}
        ],
        context_lines=[],
        gaps=[],
        evaluator_gateway=eval_gw,
    )
    assert result.verdict == "pass"
    assert len(main_provider.calls) == 0
    assert len(eval_provider.calls) >= 1


def test_world_quest_critic_fallback_on_evaluator_error() -> None:
    """WorldQuestCritic falls back to main gateway when evaluator fails."""
    error_provider = _ErrorProvider()
    main_provider = _FixedProvider(_VALID_CRITIQUE, "main")
    main_gw = _make_gateway(main_provider)
    eval_gw = _make_gateway(error_provider)

    critic = WorldQuestCritic(gateway=main_gw)
    result = critic.critique(
        brief="world brief",
        quests=[],
        context_lines=[],
        gaps=[],
        evaluator_gateway=eval_gw,
    )
    assert result is not None
    assert "[evaluator-fallback]" in result.summary
