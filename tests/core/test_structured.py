"""Structured-output layer (T8): constrain → validate → bounded repair-retry.

All offline / $0: scripted provider doubles driven through a real ``LLMGateway`` (the
``test_gateway.py`` pattern), no network and no ``OWCOPILOT_ALLOW_OFFLINE_LLM`` — a gateway built
directly from a double bypasses the offline gate, like the eval harness. The invariants under test
mirror the canonical client-side pattern (Instructor reask; OpenAI/Anthropic "validate in code"):
the repair prompt must carry the *specific* validation error, exhaustion must RAISE (never a silent
default), and truncation/transport errors must propagate untouched (never swallowed into a repair).
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from owcopilot.llm.gateway import LLMGateway, LLMGatewayError, OutputTruncatedError
from owcopilot.llm.jsonio import extract_validated
from owcopilot.llm.structured import StructuredOutputError, complete_structured


class _Toy(BaseModel):
    name: str
    count: int


class _ScriptedProvider:
    """Returns canned replies by call index; records each turn's user message."""

    def __init__(self, replies: list[str]) -> None:
        self._replies = replies
        self.calls = 0
        self.seen_users: list[str] = []

    def complete(self, *, system: str, user: str, model: str):
        self.seen_users.append(user)
        reply = self._replies[min(self.calls, len(self._replies) - 1)]
        self.calls += 1
        return reply, 1, 1


def _gateway(provider) -> LLMGateway:
    return LLMGateway(providers={"cheap": provider})


# --------------------------------------------------------------------------- happy path
def test_parses_valid_first_try() -> None:
    prov = _ScriptedProvider(['{"name": "Aldric", "count": 3}'])
    out = complete_structured(
        _gateway(prov), task="plan", system="s", user="u", model_cls=_Toy, tier="cheap"
    )
    assert out == _Toy(name="Aldric", count=3)
    assert prov.calls == 1  # no wasted repair call


# --------------------------------------------------------------------------- repair carries cause
def test_repairs_after_first_bad_reply_with_specific_error() -> None:
    prov = _ScriptedProvider(['{"name": "Aldric"}', '{"name": "Aldric", "count": 5}'])
    out = complete_structured(
        _gateway(prov), task="plan", system="s", user="ORIGINAL", model_cls=_Toy, tier="cheap"
    )
    assert out == _Toy(name="Aldric", count=5)
    assert prov.calls == 2
    repair_user = prov.seen_users[1]
    assert "ORIGINAL" in repair_user  # original request preserved
    assert "count" in repair_user  # the SPECIFIC missing field, not a generic message
    assert '{"name": "Aldric"}' in repair_user  # a snippet of the bad output is echoed back


# --------------------------------------------------------------------------- exhaustion raises
def test_raises_after_retries_exhausted_no_silent_default() -> None:
    prov = _ScriptedProvider(['{"name": "x"}'])  # always missing count
    with pytest.raises(StructuredOutputError) as excinfo:
        complete_structured(
            _gateway(prov),
            task="plan",
            system="s",
            user="u",
            model_cls=_Toy,
            tier="cheap",
            max_repair_retries=2,
        )
    msg = str(excinfo.value)
    assert "count" in msg and "_Toy" in msg
    assert prov.calls == 3  # max_repair_retries + 1
    assert excinfo.value.attempts == 3


# --------------------------------------------------------------------------- truncation propagates
def test_truncation_via_real_gateway_propagates_not_repaired() -> None:
    class _Truncating:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, *, system, user, model):
            self.calls += 1
            raise OutputTruncatedError(completion_tokens=16, cap=16)

    prov = _Truncating()
    gw = LLMGateway(providers={"cheap": prov}, max_retries=2)
    with pytest.raises(LLMGatewayError) as excinfo:
        complete_structured(gw, task="plan", system="s", user="u", model_cls=_Toy, tier="cheap")
    assert excinfo.value.category == "output_budget"
    assert not isinstance(excinfo.value, StructuredOutputError)
    assert prov.calls == 1  # fail-fast: truncation is not retried, not turned into a repair loop


def test_timeout_propagates_as_gateway_error() -> None:
    class _Timeout:
        def complete(self, *, system, user, model):
            raise TimeoutError("timed out")

    gw = LLMGateway(providers={"cheap": _Timeout()}, max_retries=0)
    with pytest.raises(LLMGatewayError) as excinfo:
        complete_structured(gw, task="plan", system="s", user="u", model_cls=_Toy, tier="cheap")
    assert excinfo.value.category == "timeout"
    assert not isinstance(excinfo.value, StructuredOutputError)


# --------------------------------------------------------------------------- extract_validated
def test_extract_validated_good_and_bad() -> None:
    assert extract_validated('{"name": "a", "count": 2}', _Toy) == _Toy(name="a", count=2)
    with pytest.raises(ValueError, match="count"):  # missing field named
        extract_validated('{"name": "a"}', _Toy)
    with pytest.raises(ValueError, match="count"):  # wrong type names the field
        extract_validated('{"name": "a", "count": "not-an-int"}', _Toy)
    with pytest.raises(ValueError):  # no JSON at all
        extract_validated("just prose, no json", _Toy)


# --------------------------------------------------------------------------- PromptSpec.schema
#
# External-acceptance finding: the registry originally left every spec's `schema` at its default
# `None` — a reviewable list, but not schema-driven. This asserted that blanket absence; it now
# asserts the opposite for the specs whose output is genuinely validated straight against one
# Pydantic model in its consuming code (`QuestLogic.model_validate`, `QAAnswer.model_validate`,
# `complete_structured(..., model_cls=SufficiencyVerdict)`, etc. — see prompts/__init__.py for the
# per-spec citations) — while every prompt whose output is free text or hand-parsed/merged/reshaped
# (no single model matches the JSON shape) correctly stays `None`.
def test_prompt_spec_schema_field_is_optional() -> None:
    from owcopilot.prompts import PROMPT_SPECS, prompt_registry

    reg = prompt_registry()
    assert len(reg) == len(PROMPT_SPECS)
    with_schema = [spec for spec in PROMPT_SPECS if spec.schema is not None]
    assert len(with_schema) >= 7, (
        "expected at least the 7 audited specs (assist.critic.verdict_shape/system, "
        "logic.draft.system, qa.answer.system, qa.community.cluster/global, "
        "retrieval.agentic.sufficiency) to carry a schema"
    )
    for spec in with_schema:
        model_cls = spec.resolve_schema()
        assert isinstance(model_cls, type) and issubclass(model_cls, BaseModel), (
            f"{spec.name}: schema {spec.schema!r} must resolve to a BaseModel subclass"
        )
        # the model must actually be serializable (a real, usable JSON schema for tooling/docs)
        json_schema = model_cls.model_json_schema()
        assert isinstance(json_schema, dict) and json_schema.get("type") == "object"
