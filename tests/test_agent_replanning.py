# T5 plan-and-execute 重规划验收测试。全部 $0 离线,驱动真实 ReActAgent.run / create_agent 循环 +
# 真实 reflexion.distill_reflection(离线确定性模板),证明:重规划真被触发、上限 2 轮、与 Reflexion
# 组合(蒸馏反思进入新 attempt 的输入)、不破坏 T4 resume、一次成功逐字节等价关闭时的结果。
"""T5 plan-and-execute replanning acceptance tests.

Every test is $0/offline and drives the REAL production code — ``ReActAgent.run`` over the standard
``create_agent`` loop and the REAL ``reflexion.distill_reflection`` (its offline deterministic
template, enabled for the suite by conftest). They pin the six things the design promises:

* replanning is actually TRIGGERED when a first attempt ends unsuccessfully, and a later attempt's
  success is the returned result (A1);
* the reflection distilled from the failed attempt ENTERS the next attempt's prompt — proof it is
  composed WITH Reflexion, not instead of it (A1);
* it is BOUNDED at ``max_replans`` (cap 2) — a forever-failing run never runs more attempts (A1);
* it does NOT disturb T4 resume (a resume takes the single-attempt path, replans stay 0) (A1);
* a first-attempt success with replanning ON is byte-identical to replanning OFF (A2);
* ``max_replans`` defaults to 0 (off) and is validated against the cap.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage
from pydantic import Field

from owcopilot.agent import ReActAgent, ReflexionError
from owcopilot.fakes import scripted_chat_model
from owcopilot.skills import CostTier, SideEffect, Skill, SkillRegistry
from owcopilot.storage.checkpointer import CheckpointStore

# --------------------------------------------------------------------------- doubles / helpers


def _probe_registry() -> SkillRegistry:
    """A registry with one read-only, deterministic ``probe`` skill — enough to drive the loop
    without opening a content project (the tools are irrelevant to replanning, only the outcome is).
    """
    registry = SkillRegistry()
    registry.register(
        Skill(
            name="probe",
            description="Returns a fixed result (read-only, deterministic).",
            cost_tier=CostTier.DETERMINISTIC,
            side_effect=SideEffect.READ_ONLY,
            handler=lambda **_kw: {"ok": True},
            parameters=(),
        )
    )
    return registry


class _RecordingChatModel(GenericFakeChatModel):
    """Scripted fake that records the exact message list of EVERY model call, so a test can assert
    which attempt's system prompt carried the injected reflection block."""

    seen: list[list[BaseMessage]] = Field(default_factory=list)

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Any:
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(list(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


class _CrashAfterScript(GenericFakeChatModel):
    """Replays its scripted turns, then RAISES on the next call — a controllable mid-run crash that
    leaves a resumable checkpoint (mirrors the T4 checkpointer test's double)."""

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Any:
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        try:
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        except StopIteration:
            raise RuntimeError("crash-mid-run") from None


def _probe_call(call_id: str) -> dict[str, Any]:
    return {"name": "probe", "args": {}, "id": call_id, "type": "tool_call"}


_REFLECTION_HEADER = "Reflections from previous attempts:"


# --------------------------------------------------------------------------- A1: trigger + result


def test_replanning_triggers_and_second_attempt_succeeds() -> None:
    # Attempt 0 keeps calling a tool and exhausts the 2-step budget (max_steps); replanning then
    # runs a SECOND attempt that finishes. The returned result is the successful second attempt, and
    # replan counter records exactly one replan. The two attempts share one scripted model iterator,
    # so the script flows across them (offline distill makes no model call in between).
    model = scripted_chat_model(
        [
            ("probe", {}), ("probe", {}),   # attempt 0: exhaust max_steps=2 → max_steps
            ("probe", {}), "Report: done",  # attempt 1: probe once, then a real final answer
        ]
    )
    agent = ReActAgent(model=model, registry=_probe_registry(), max_steps=2, max_replans=2)

    result = agent.run("diagnose the world")

    assert result.replans == 1
    assert result.stop_reason == "finished"
    assert result.final_answer == "Report: done"
    assert result.step_count == 1  # only the second attempt's single probe step is reported


def test_replan_reflection_enters_second_attempt_input() -> None:
    # Composition with Reflexion: the reflection distilled from attempt 0's failure must reach
    # attempt 1's prompt (via MemoryInjectionMiddleware). Attempt 0's first call carries NO
    # reflection block; attempt 1's first call carries the block AND the distilled reflection text.
    model = _RecordingChatModel(
        messages=iter(
            [
                AIMessage(content="", tool_calls=[_probe_call("c0")]),
                AIMessage(content="", tool_calls=[_probe_call("c1")]),  # attempt 0 exhausts budget
                AIMessage(content="", tool_calls=[_probe_call("c2")]),
                AIMessage(content="report"),  # attempt 1 finishes
            ]
        )
    )
    agent = ReActAgent(model=model, registry=_probe_registry(), max_steps=2, max_replans=2)

    result = agent.run("diagnose")

    assert result.replans == 1
    # seen[0], seen[1] belong to attempt 0; seen[2], seen[3] to attempt 1 (offline distill is $0/no
    # model call, so the indices are contiguous across the two attempts).
    assert _REFLECTION_HEADER not in model.seen[0][0].text
    assert _REFLECTION_HEADER in model.seen[2][0].text
    # The block carries the REAL distilled reflection (offline template keyed on the failed trial),
    # proving it is the Reflexion Self-Reflection output that fed the replan, not a placeholder.
    assert "第1次尝试未通过" in model.seen[2][0].text


def test_replan_composes_with_recalled_reflections() -> None:
    # A recalled reflection passed into run() conditions attempt 0 AND is still present next to the
    # freshly distilled reflection in attempt 1 — the replan loop ACCUMULATES on top of recalled
    # memory (composition, not replacement).
    model = _RecordingChatModel(
        messages=iter(
            [
                AIMessage(content="", tool_calls=[_probe_call("c0")]),
                AIMessage(content="", tool_calls=[_probe_call("c1")]),
                AIMessage(content="", tool_calls=[_probe_call("c2")]),
                AIMessage(content="report"),
            ]
        )
    )
    agent = ReActAgent(model=model, registry=_probe_registry(), max_steps=2, max_replans=2)

    agent.run("diagnose", reflections=["recalled: always probe first"])

    assert "recalled: always probe first" in model.seen[0][0].text  # attempt 0 sees the recall
    attempt1_system = model.seen[2][0].text
    assert "recalled: always probe first" in attempt1_system  # still there
    assert "第1次尝试未通过" in attempt1_system  # plus the distilled replan reflection


def test_replanning_is_bounded_at_two_rounds() -> None:
    # A model that NEVER finishes: every attempt exhausts the budget. With max_replans=2 there are
    # exactly 1 + 2 = 3 attempts (6 tool-call turns); replanning never runs a fourth attempt.
    model = scripted_chat_model([("probe", {})] * 6)
    agent = ReActAgent(model=model, registry=_probe_registry(), max_steps=2, max_replans=2)

    result = agent.run("never finishes")

    assert result.replans == 2  # capped at max_replans, not more
    assert result.stop_reason == "max_steps"  # the last attempt still failed


def test_replan_stops_when_self_reflection_fails(monkeypatch) -> None:
    # No silent downgrade: if the Self-Reflection distiller cannot produce a real reflection
    # (ReflexionError), replanning STOPS and the last real result is returned — it does not replan
    # on an empty reflection. distill_reflection is imported lazily in the loop, so patching it on
    # the reflexion module takes effect.
    def _boom(**_kwargs: Any) -> str:
        raise ReflexionError("blank reflection")

    monkeypatch.setattr("owcopilot.agent.reflexion.distill_reflection", _boom)
    model = scripted_chat_model([("probe", {}), ("probe", {})])  # only attempt 0's turns
    agent = ReActAgent(model=model, registry=_probe_registry(), max_steps=2, max_replans=2)

    result = agent.run("diagnose")

    assert result.replans == 0  # no replan happened — could not distil a reflection
    assert result.stop_reason == "max_steps"


# --------------------------------------------------------------------------- A1: resume untouched


def test_resume_without_checkpointer_still_rejected_with_replanning_enabled() -> None:
    # Routing proof: a resume must take the single-attempt path regardless of max_replans, so a
    # resume without a checkpointer fails with the SAME guided error as before (never enters the
    # replanning wrapper).
    agent = ReActAgent(
        model=scripted_chat_model(["x"]), registry=_probe_registry(), max_replans=2
    )
    with pytest.raises(ValueError, match="checkpointer"):
        agent.run(resume=True)


def test_crash_then_resume_works_with_replanning_enabled(tmp_path) -> None:
    # T4 resume must be untouched by T5. A run crashes mid-loop and is resumed on the same thread
    # with a replanning-enabled agent; the resumed result matches the clean run, the tool is not
    # re-run, and replans stays 0 (a resume continues one attempt — it never replans).
    store = CheckpointStore(tmp_path / "cp.sqlite")
    try:
        clean = ReActAgent(
            model=scripted_chat_model([("probe", {}), "final: all good"]),
            registry=_probe_registry(),
            checkpointer=store.saver,
            max_replans=2,
        ).run("check the world", thread_id="clean-1")
        assert clean.replans == 0  # succeeded on attempt 0

        crash_registry = _probe_registry()
        crash_model = _CrashAfterScript(
            messages=iter([AIMessage(content="", tool_calls=[_probe_call("c0")])])
        )
        crash_agent = ReActAgent(
            model=crash_model,
            registry=crash_registry,
            checkpointer=store.saver,
            max_replans=2,
        )
        with pytest.raises(RuntimeError, match="crash-mid-run"):
            crash_agent.run("check the world", thread_id="t1")

        resumed = ReActAgent(
            model=scripted_chat_model(["final: all good"]),
            registry=crash_registry,
            checkpointer=store.saver,
            max_replans=2,
        ).run(thread_id="t1", resume=True)  # goal omitted — recovered from the checkpoint
    finally:
        store.close()

    assert resumed.final_answer == clean.final_answer
    assert [s.action for s in resumed.steps] == [s.action for s in clean.steps]
    assert resumed.replans == 0  # resume is exempt from replanning
    assert resumed.goal == "check the world"


# --------------------------------------------------------------------------- A2: byte-identical


def test_first_attempt_success_is_byte_identical_to_disabled() -> None:
    # A2: enabling replanning must not change a run that succeeds on the first attempt — the result
    # is byte-identical to the same run with replanning off. Same deterministic script +
    # registry, so the ONLY variable is max_replans.
    script: list[Any] = [("probe", {}), "final: ok"]
    disabled = ReActAgent(
        model=scripted_chat_model(list(script)),
        registry=_probe_registry(),
        max_steps=6,
        max_replans=0,
    ).run("check the world")
    enabled = ReActAgent(
        model=scripted_chat_model(list(script)),
        registry=_probe_registry(),
        max_steps=6,
        max_replans=2,
    ).run("check the world")

    assert enabled.replans == 0
    assert enabled.model_dump(mode="json") == disabled.model_dump(mode="json")


# --------------------------------------------------------------------------- construction / bounds


def test_max_replans_defaults_to_zero() -> None:
    agent = ReActAgent(model=scripted_chat_model(["x"]), registry=_probe_registry())
    assert agent.max_replans == 0


def test_max_replans_out_of_bounds_is_rejected() -> None:
    for bad in (-1, 3, 10):
        with pytest.raises(ValueError, match="max_replans"):
            ReActAgent(
                model=scripted_chat_model(["x"]),
                registry=_probe_registry(),
                max_replans=bad,
            )
