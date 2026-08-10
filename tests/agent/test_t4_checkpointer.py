"""T4 断点续跑 (checkpointer + resume) 验收测试.

Covers, all $0 offline against LangChain-standard doubles + the REAL production code
(``ReActAgent`` / ``MultiAgentSession`` / ``CheckpointStore`` / the CLI ``agent`` command):

* CheckpointStore lifecycle — independent ``checkpoints.sqlite`` file, the self-managed thread
  registry, and the TTL / newest-N sweep that bounds it (production-grade cleanup).
* ``resolve_thread_id`` policy (auto-mint, resume validation, no-checkpointer misuse).
* Single-agent parity — a checkpointer + fresh thread_id produces a byte-identical ``AgentResult``
  vs no checkpointer (the "default zero behaviour change" guarantee).
* **A1 core** — a run crashes mid-loop, resumes on the same thread_id, and the result is identical
  while an already-run tool does NOT re-run (LangGraph replays its pending write).
* CLI ``agent`` — surfaces the thread_id, writes the checkpoint file, and the §5.2 guided errors.
* Multi-agent (scheme Y) — checkpointer parity + wiring, and the interrupt/Command round-trip that
  proves the T4 infra is exactly what T6 in-process HITL reuses.
"""

from __future__ import annotations

import json
import operator
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, SystemMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from typing_extensions import TypedDict

from owcopilot.agent import ReActAgent
from owcopilot.agent.middleware import RUN_POLICY_VERSION, AgentRunContractError
from owcopilot.agent.offline import OfflineReactChatModel
from owcopilot.agent.reflexion import distill_reflection
from owcopilot.app.actions.agent import run_agent_diagnose_action
from owcopilot.cli.main import main
from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest
from owcopilot.content.store import ContentStore
from owcopilot.fakes import scripted_chat_model
from owcopilot.llm.telemetry import TelemetryCollector
from owcopilot.skills import CostTier, SideEffect, Skill, SkillRegistry, default_skill_registry
from owcopilot.storage.checkpointer import (
    CheckpointModelBudgetLedger,
    CheckpointStore,
    resolve_thread_id,
)

# --------------------------------------------------------------------------- doubles / helpers


class _CrashAfterScript(GenericFakeChatModel):
    """Replays its scripted turns, then RAISES on the next call — a controllable mid-run crash.

    Model exceptions propagate out of ``agent.invoke`` (the tool-error recovery net only catches
    TOOL exceptions), so this aborts the run and leaves a resumable checkpoint at the last
    completed superstep."""

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Any:
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        try:
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        except StopIteration:
            raise RuntimeError("crash-mid-run") from None


class _FatalDispatchCrash(BaseException):
    """Simulates process death: ordinary ``except Exception`` fallbacks cannot swallow it."""


class _CrashOnAuxiliaryCall(GenericFakeChatModel):
    crash_marker: str

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Any:
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        system = messages[0] if messages and isinstance(messages[0], SystemMessage) else None
        if system is not None and self.crash_marker in str(system.text):
            raise _FatalDispatchCrash(self.crash_marker)
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def _probe_registry() -> tuple[SkillRegistry, list[str]]:
    """A registry with one read-only ``probe`` skill that records each invocation."""
    calls: list[str] = []
    registry = SkillRegistry()
    registry.register(
        Skill(
            name="probe",
            description="Records one call (read-only, deterministic).",
            cost_tier=CostTier.DETERMINISTIC,
            side_effect=SideEffect.READ_ONLY,
            handler=lambda **_kw: (calls.append("probe"), {"ok": True})[1],
            parameters=(),
        )
    )
    return registry, calls


def _dirty_project(content_root) -> None:
    ContentStore(content_root).save(
        ContentBundle(
            entities={
                "npc_aldric": Entity(
                    id="npc_aldric", name="Aldric", type=EntityType.NPC, description="Caravan boss"
                )
            },
            quests={"q1": Quest(id="q1", title="Q1", giver_npc="npc_missing")},
        )
    )


# ----------------------------------------------------------------- CheckpointStore lifecycle


def test_checkpoint_store_creates_independent_file_and_registry(tmp_path) -> None:
    path = tmp_path / ".owcopilot" / "checkpoints.sqlite"
    store = CheckpointStore(path)
    try:
        assert path.exists()  # created (mkdir parents)
        tables = {
            row[0]
            for row in store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        # The saver's tables plus our lifecycle registry all live in this ONE independent file.
        assert {"checkpoints", "writes", "checkpoint_threads"} <= tables
    finally:
        store.close()


def test_record_thread_is_upsert(tmp_path) -> None:
    store = CheckpointStore(tmp_path / "cp.sqlite")
    try:
        store.record_thread("t1", source="agent")
        first = store.list_threads()
        store.record_thread("t1", source="agent")  # touch again
        rows = store.list_threads()
        assert len(rows) == 1  # upsert, not a second row
        assert rows[0]["thread_id"] == "t1"
        assert rows[0]["last_touched_at"] >= first[0]["last_touched_at"]
    finally:
        store.close()


def test_sweep_ttl_expired_deletes_checkpoint_and_registry(tmp_path) -> None:
    store = CheckpointStore(tmp_path / "cp.sqlite")
    try:
        registry, _ = _probe_registry()
        # A real run writes a real checkpoint under "old-1".
        ReActAgent(
            model=scripted_chat_model([("probe", {}), "done"]),
            registry=registry,
            checkpointer=store.saver,
        ).run("goal", thread_id="old-1")
        store.record_thread("old-1", source="agent")
        store.record_thread("fresh-1", source="agent")
        assert store.thread_exists("old-1")

        # Backdate "old-1" well past the 14-day TTL, then sweep.
        stale = (datetime.now(UTC) - timedelta(days=30)).isoformat()
        store._conn.execute(
            "UPDATE checkpoint_threads SET last_touched_at = ? WHERE thread_id = ?",
            (stale, "old-1"),
        )
        store._conn.commit()

        removed = store.sweep()
        assert removed == 1
        assert not store.thread_exists("old-1")  # checkpoint rows gone
        assert {r["thread_id"] for r in store.list_threads()} == {"fresh-1"}  # registry row gone
    finally:
        store.close()


def test_sweep_over_cap_keeps_newest(tmp_path) -> None:
    store = CheckpointStore(tmp_path / "cp.sqlite", ttl_seconds=0, max_threads=2)
    try:
        for i, tid in enumerate(["a", "b", "c"]):
            store.record_thread(tid, source="agent")
            # Force distinct, increasing timestamps so ordering is deterministic (a oldest).
            ts = (datetime.now(UTC) + timedelta(seconds=i)).isoformat()
            store._conn.execute(
                "UPDATE checkpoint_threads SET last_touched_at = ? WHERE thread_id = ?", (ts, tid)
            )
        store._conn.commit()

        removed = store.sweep()
        assert removed == 1  # over the cap of 2 by one
        assert {r["thread_id"] for r in store.list_threads()} == {"b", "c"}  # newest two kept
    finally:
        store.close()


# ----------------------------------------------------------------- resolve_thread_id policy


def test_resolve_thread_id_without_checkpointer_rejects_thread_or_resume() -> None:
    assert resolve_thread_id(None, thread_id=None, resume=False) is None  # default: no-op
    with pytest.raises(ValueError, match="checkpointer"):
        resolve_thread_id(None, thread_id="t1", resume=False)
    with pytest.raises(ValueError, match="checkpointer"):
        resolve_thread_id(None, thread_id=None, resume=True)


def test_resolve_thread_id_with_checkpointer(tmp_path) -> None:
    store = CheckpointStore(tmp_path / "cp.sqlite")
    try:
        minted = resolve_thread_id(store.saver, thread_id=None, resume=False)
        assert minted and len(minted) >= 8  # a fresh UUID
        assert resolve_thread_id(store.saver, thread_id="pinned", resume=False) == "pinned"
        assert resolve_thread_id(store.saver, thread_id="t1", resume=True) == "t1"
        with pytest.raises(ValueError, match="resume"):
            resolve_thread_id(store.saver, thread_id=None, resume=True)
    finally:
        store.close()


# --------------------------------------------------------------------------- single-agent parity


def test_checkpointer_fresh_thread_is_byte_identical() -> None:
    # §1.4: attaching a checkpointer with a fresh unique thread_id must not change the produced
    # AgentResult vs no checkpointer at all — the default-zero-behaviour-change guarantee. Uses the
    # deterministic probe registry + a scripted model so the ONLY variable is the checkpointer
    # (the real audit tool embeds run-specific data, which would mask the comparison).
    reg_plain, _ = _probe_registry()
    plain = ReActAgent(
        model=scripted_chat_model([("probe", {}), "final: ok"]), registry=reg_plain
    ).run("check the world")

    store = CheckpointStore(":memory:")
    try:
        reg_cp, _ = _probe_registry()
        checkpointed = ReActAgent(
            model=scripted_chat_model([("probe", {}), "final: ok"]),
            registry=reg_cp,
            checkpointer=store.saver,
        ).run("check the world", thread_id="fresh-1")
    finally:
        store.close()

    assert checkpointed.model_dump(mode="json") == plain.model_dump(mode="json")


def test_default_run_carries_no_thread_id() -> None:
    # No checkpointer ⇒ last_thread_id stays None and the config carries no thread_id (pre-T4).
    registry, _ = _probe_registry()
    agent = ReActAgent(model=scripted_chat_model([("probe", {}), "done"]), registry=registry)
    agent.run("goal")
    assert agent.last_thread_id is None


# ----------------------------------------------------------------- A1 core: crash -> resume


def test_crash_then_resume_is_consistent_and_does_not_rerun_tools(tmp_path) -> None:
    # Reference clean run (checkpointer + fresh thread).
    clean_registry, clean_calls = _probe_registry()
    store = CheckpointStore(tmp_path / "cp.sqlite")
    try:
        clean = ReActAgent(
            model=scripted_chat_model([("probe", {}), "final: all good"]),
            registry=clean_registry,
            checkpointer=store.saver,
        ).run("check the world", thread_id="clean-1")
        assert clean_calls == ["probe"]

        # Crash run: emit the probe call, execute it, then the model raises on the next turn.
        crash_registry, crash_calls = _probe_registry()
        probe_turn = AIMessage(
            content="",
            tool_calls=[{"name": "probe", "args": {}, "id": "c0", "type": "tool_call"}],
        )
        crash_model = _CrashAfterScript(messages=iter([probe_turn]))
        crash_agent = ReActAgent(
            model=crash_model, registry=crash_registry, checkpointer=store.saver
        )
        with pytest.raises(RuntimeError, match="crash-mid-run"):
            crash_agent.run("check the world", thread_id="t1")
        assert crash_calls == ["probe"]  # probe ran once, during the crashed run

        # Resume on the SAME thread with a fresh graph + a model that just finishes.
        resumed = ReActAgent(
            model=scripted_chat_model(["final: all good"]),
            registry=crash_registry,
            checkpointer=store.saver,
        ).run(thread_id="t1", resume=True)  # goal omitted — recovered from the checkpoint
    finally:
        store.close()

    assert crash_calls == ["probe"]  # NOT re-run on resume (its pending write was replayed)
    assert resumed.final_answer == clean.final_answer
    assert [s.action for s in resumed.steps] == [s.action for s in clean.steps]
    assert resumed.step_count == clean.step_count
    assert resumed.goal == "check the world"  # recovered from the persisted first message


def test_exact_no_progress_guard_hydrates_from_checkpoint_on_resume(tmp_path) -> None:
    registry, calls = _probe_registry()
    store = CheckpointStore(tmp_path / "no-progress.sqlite")
    try:
        turns = [
            AIMessage(
                content="",
                tool_calls=[{"name": "probe", "args": {}, "id": "c0", "type": "tool_call"}],
            ),
            AIMessage(
                content="",
                tool_calls=[{"name": "probe", "args": {}, "id": "c1", "type": "tool_call"}],
            ),
        ]
        crashing = ReActAgent(
            model=_CrashAfterScript(messages=iter(turns)),
            registry=registry,
            max_steps=6,
            max_tool_calls=10,
            checkpointer=store.saver,
        )
        with pytest.raises(RuntimeError, match="crash-mid-run"):
            crashing.run("probe until stable", thread_id="np-1")
        assert calls == ["probe", "probe"]

        resumed = ReActAgent(
            model=scripted_chat_model([("probe", {}), "final: stable"]),
            registry=registry,
            max_steps=6,
            max_tool_calls=10,
            checkpointer=store.saver,
        ).run(thread_id="np-1", resume=True)
    finally:
        store.close()

    assert calls == ["probe", "probe"], "the third exact call must be blocked before execution"
    assert resumed.final_answer == "final: stable"
    assert json.loads(resumed.steps[-1].observation)["error_code"] == "NO_PROGRESS"


def test_period_two_semantic_cycle_guard_hydrates_from_checkpoint_on_resume(tmp_path) -> None:
    calls: list[str] = []
    registry = SkillRegistry()
    for name in ("cycle_a", "cycle_b"):
        registry.register(
            Skill(
                name=name,
                description=f"stable {name}",
                cost_tier=CostTier.DETERMINISTIC,
                side_effect=SideEffect.READ_ONLY,
                handler=lambda _name=name: (
                    calls.append(_name),
                    {"value": _name},
                )[1],
            )
        )
    store = CheckpointStore(tmp_path / "cycle-resume.sqlite")
    try:
        names = ["cycle_a", "cycle_b", "cycle_a", "cycle_b"]
        turns = [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": name, "args": {}, "id": f"cycle-c{i}", "type": "tool_call"}
                ],
            )
            for i, name in enumerate(names)
        ]
        with pytest.raises(RuntimeError, match="crash-mid-run"):
            ReActAgent(
                model=_CrashAfterScript(messages=iter(turns)),
                registry=registry,
                max_steps=10,
                max_model_calls=20,
                max_tool_calls=20,
                checkpointer=store.saver,
            ).run("detect periodic loop", thread_id="cycle-resume")
        assert calls == names

        resumed = ReActAgent(
            model=scripted_chat_model([("cycle_a", {}), "done"]),
            registry=registry,
            checkpointer=store.saver,
        ).run(thread_id="cycle-resume", resume=True)
    finally:
        store.close()

    assert calls == names
    assert json.loads(resumed.steps[-1].observation)["error_code"] == "NO_PROGRESS"


def test_tool_call_thread_budget_survives_crash_and_resume(tmp_path) -> None:
    registry, calls = _probe_registry()
    store = CheckpointStore(tmp_path / "tool-budget.sqlite")
    try:
        turns = [
            AIMessage(
                content="",
                tool_calls=[{"name": "probe", "args": {}, "id": "c0", "type": "tool_call"}],
            ),
            AIMessage(
                content="",
                tool_calls=[{"name": "probe", "args": {}, "id": "c1", "type": "tool_call"}],
            ),
        ]
        with pytest.raises(RuntimeError, match="crash-mid-run"):
            ReActAgent(
                model=_CrashAfterScript(messages=iter(turns)),
                registry=registry,
                max_steps=6,
                max_tool_calls=2,
                checkpointer=store.saver,
            ).run("bounded probes", thread_id="budget-1")
        assert calls == ["probe", "probe"]

        resumed = ReActAgent(
            model=scripted_chat_model([("probe", {}), "final: budget reached"]),
            registry=registry,
            max_steps=6,
            max_tool_calls=2,
            checkpointer=store.saver,
        ).run(thread_id="budget-1", resume=True)
    finally:
        store.close()

    assert calls == ["probe", "probe"]
    envelope = json.loads(resumed.steps[-1].observation)
    assert envelope["error_code"] == "BUDGET_EXCEEDED"


def test_resume_omitted_limits_restore_versioned_checkpoint_contract(tmp_path) -> None:
    registry, _ = _probe_registry()
    store = CheckpointStore(tmp_path / "contract.sqlite")
    try:
        with pytest.raises(RuntimeError, match="crash-mid-run"):
            ReActAgent(
                model=_CrashAfterScript(
                    messages=iter(
                        [
                            AIMessage(
                                content="",
                                tool_calls=[
                                    {
                                        "name": "probe",
                                        "args": {},
                                        "id": "contract-c0",
                                        "type": "tool_call",
                                    }
                                ],
                            )
                        ]
                    )
                ),
                registry=registry,
                max_steps=4,
                max_model_calls=9,
                max_tool_calls=7,
                checkpointer=store.saver,
            ).run("freeze limits", thread_id="contract-1")

        values = store.saver.get(
            {"configurable": {"thread_id": "contract-1"}}
        )["channel_values"]
        assert values["owc_run_contract"] == {
            "schema_version": 3,
            "policy_version": RUN_POLICY_VERSION,
            "max_steps": 4,
            "max_model_calls": 9,
            "max_cost_usd": None,
            "max_tool_calls": 7,
            "tool_transport": "native",
            "budget_ledger_id": "contract-1",
        }
        assert values["owc_total_model_calls"] == 1

        resumed_agent = ReActAgent(
            model=scripted_chat_model(["done"]),
            registry=registry,
            checkpointer=store.saver,
        )
        resumed = resumed_agent.run(thread_id="contract-1", resume=True)
    finally:
        store.close()

    assert resumed.final_answer == "done"
    assert resumed_agent.max_steps == 4
    assert resumed_agent.max_model_calls == 9
    assert resumed_agent.max_tool_calls == 7
    assert resumed_agent.tool_transport == "native"
    assert resumed_agent._active_total_model_budget is not None
    assert resumed_agent._active_total_model_budget.used == 3


@pytest.mark.parametrize(
    ("changed", "value"),
    [
        ("max_model_calls", 10),
        ("max_cost_usd", 0.2),
        ("max_tool_calls", 8),
        ("max_steps", 5),
        ("tool_transport", "mcp"),
    ],
)
def test_resume_rejects_changed_frozen_limit(tmp_path, changed, value) -> None:
    registry, _ = _probe_registry()
    store = CheckpointStore(tmp_path / f"mismatch-{changed}.sqlite")
    try:
        with pytest.raises(RuntimeError, match="crash-mid-run"):
            ReActAgent(
                model=_CrashAfterScript(messages=iter([])),
                registry=registry,
                max_steps=4,
                max_model_calls=9,
                max_cost_usd=0.5,
                max_tool_calls=7,
                checkpointer=store.saver,
            ).run("freeze", thread_id="mismatch")
        kwargs = {changed: value}
        with pytest.raises(AgentRunContractError) as excinfo:
            ReActAgent(
                model=scripted_chat_model(["must not run"]),
                registry=registry,
                checkpointer=store.saver,
                **kwargs,
            ).run(thread_id="mismatch", resume=True)
    finally:
        store.close()

    envelope = json.loads(str(excinfo.value))
    assert envelope["error_code"] == "RUN_CONTRACT_MISMATCH"
    assert changed in envelope["message"]


def test_resume_restores_cumulative_cost_and_blocks_before_new_collector_dispatch(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("OWCOPILOT_PRICE_CHEAP", raising=False)
    registry, calls = _probe_registry()
    store = CheckpointStore(tmp_path / "cost-resume.sqlite")
    first_message = AIMessage(
        content="",
        tool_calls=[
            {"name": "probe", "args": {}, "id": "cost-resume-c1", "type": "tool_call"}
        ],
        usage_metadata={
            "input_tokens": 1_000_000,
            "output_tokens": 0,
            "total_tokens": 1_000_000,
        },
    )
    try:
        paused = ReActAgent(
            model=_CrashAfterScript(messages=iter([first_message])),
            registry=registry,
            max_steps=4,
            max_cost_usd=0.30,
            telemetry=TelemetryCollector(),
            checkpointer=store.saver,
            interrupt_before={"probe"},
        ).run("pause after paid proposal", thread_id="cost-resume")
        assert paused.interrupted is True
        values = store.saver.get(
            {"configurable": {"thread_id": "cost-resume"}}
        )["channel_values"]
        assert values["owc_settled_cost_usd"] == pytest.approx(0.30)
        assert values["owc_cost_is_estimate"] is True

        # A fresh collector starts at $0, but the frozen contract/channel restore the cumulative
        # $0.30. The approved tool runs once; the next planning model call is blocked before this
        # empty script can dispatch.
        resumed_agent = ReActAgent(
            model=_CrashAfterScript(messages=iter([])),
            registry=registry,
            telemetry=TelemetryCollector(),
            checkpointer=store.saver,
            interrupt_before={"probe"},
        )
        resumed = resumed_agent.run(
            thread_id="cost-resume",
            resume=True,
            resume_decisions=[{"type": "approve"}],
        )
    finally:
        store.close()

    assert calls == ["probe"]
    assert resumed.stop_reason == "max_cost"
    assert json.loads(resumed.final_answer)["error_code"] == "BUDGET_EXCEEDED"
    assert resumed_agent.max_cost_usd == pytest.approx(0.30)
    assert resumed_agent.settled_cost_usd == pytest.approx(0.30)
    assert resumed_agent.cost_is_estimate is True


def test_action_resume_reports_cumulative_cost_separately_from_invocation_cost(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("OWCOPILOT_PRICE_CHEAP", raising=False)
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    checkpoint_path = str(tmp_path / "action-cost-checkpoints.sqlite")
    sqlite_path = str(tmp_path / "action-cost-runtime.sqlite")
    first_message = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "propose_fix",
                "args": {"issue_id": "missing"},
                "id": "action-cost-c1",
                "type": "tool_call",
            }
        ],
        usage_metadata={
            "input_tokens": 1_000_000,
            "output_tokens": 0,
            "total_tokens": 1_000_000,
        },
    )
    paused = run_agent_diagnose_action(
        content_root,
        goal="propose a reviewed fix",
        hitl=True,
        max_cost_usd=0.30,
        checkpoint_path=checkpoint_path,
        sqlite_path=sqlite_path,
        chat_model=_CrashAfterScript(messages=iter([first_message])),
    )
    assert paused["status"] == "interrupted"

    resumed = run_agent_diagnose_action(
        content_root,
        thread_id=paused["thread_id"],
        resume=True,
        decision="approve",
        checkpoint_path=checkpoint_path,
        sqlite_path=sqlite_path,
        chat_model=_CrashAfterScript(messages=iter([])),
    )

    assert resumed["stop_reason"] == "max_cost"
    assert resumed["telemetry"]["total_cost_usd"] == 0.0  # this invocation's new collector
    assert resumed["cost_budget"]["budget_usd"] == pytest.approx(0.30)
    assert resumed["cost_budget"]["used_usd"] == pytest.approx(0.30)
    assert resumed["run_budget"]["invocation_cost_usd"] == 0.0
    assert resumed["run_budget"]["settled_cost_usd"] == pytest.approx(0.30)
    assert resumed["run_budget"]["cumulative_gate_exhausted"] is True
    assert resumed["run_budget"]["cost_is_estimate"] is True
    assert resumed["run_budget"]["pricing_source"] == "illustrative_default_or_mixed"
    assert resumed["run_budget"]["hard_usd_cap"] is False


def test_resume_rejects_policy_version_change_before_model_call(tmp_path, monkeypatch) -> None:
    registry, _ = _probe_registry()
    store = CheckpointStore(tmp_path / "policy.sqlite")
    try:
        with pytest.raises(RuntimeError, match="crash-mid-run"):
            ReActAgent(
                model=_CrashAfterScript(messages=iter([])),
                registry=registry,
                checkpointer=store.saver,
            ).run("freeze", thread_id="policy-1")
        monkeypatch.setattr("owcopilot.agent.react.RUN_POLICY_VERSION", "future-policy-v999")
        with pytest.raises(AgentRunContractError, match="RUN_CONTRACT_MISMATCH"):
            ReActAgent(
                model=scripted_chat_model(["must not run"]),
                registry=registry,
                checkpointer=store.saver,
            ).run(thread_id="policy-1", resume=True)
    finally:
        store.close()


def test_planning_thread_budget_survives_crash_and_resume(tmp_path) -> None:
    registry, calls = _probe_registry()
    store = CheckpointStore(tmp_path / "model-thread.sqlite")
    try:
        first_turn = AIMessage(
            content="",
            tool_calls=[
                {"name": "probe", "args": {}, "id": "model-c0", "type": "tool_call"}
            ],
        )
        with pytest.raises(RuntimeError, match="crash-mid-run"):
            ReActAgent(
                model=_CrashAfterScript(messages=iter([first_turn])),
                registry=registry,
                max_steps=2,
                max_model_calls=10,
                checkpointer=store.saver,
            ).run("bounded model", thread_id="model-budget")
        assert calls == ["probe"]

        resumed = ReActAgent(
            model=_CrashAfterScript(messages=iter([])),  # would raise if a model call leaked
            registry=registry,
            checkpointer=store.saver,
        ).run(thread_id="model-budget", resume=True)
    finally:
        store.close()

    assert resumed.stop_reason == "max_steps"
    assert calls == ["probe"]


def test_external_revision_change_on_resume_prevents_stale_no_progress_pre_kill(tmp_path) -> None:
    state = {"revision": "rev-1"}
    calls: list[str] = []
    registry = SkillRegistry()
    registry.register(
        Skill(
            name="probe",
            description="revision-aware stable probe",
            cost_tier=CostTier.DETERMINISTIC,
            side_effect=SideEffect.READ_ONLY,
            handler=lambda: (calls.append(state["revision"]), {"finding": "same"})[1],
            semantic_projection=lambda result: result,
            semantic_projection_name="test:stable-v1",
            state_marker=lambda: state["revision"],
            state_marker_policy="test-revision-v1",
        )
    )
    store = CheckpointStore(tmp_path / "revision.sqlite")
    try:
        turns = [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "probe", "args": {}, "id": f"revision-c{i}", "type": "tool_call"}
                ],
            )
            for i in range(2)
        ]
        with pytest.raises(RuntimeError, match="crash-mid-run"):
            ReActAgent(
                model=_CrashAfterScript(messages=iter(turns)),
                registry=registry,
                max_model_calls=10,
                max_tool_calls=10,
                checkpointer=store.saver,
            ).run("observe stable state", thread_id="revision-1")
        assert calls == ["rev-1", "rev-1"]

        state["revision"] = "rev-2"
        resumed = ReActAgent(
            model=scripted_chat_model([("probe", {}), "done"]),
            registry=registry,
            checkpointer=store.saver,
        ).run(thread_id="revision-1", resume=True)
    finally:
        store.close()

    assert calls == ["rev-1", "rev-1", "rev-2"]
    assert resumed.steps[-1].is_error is False
    assert resumed.final_answer == "done"


def test_replan_attempt_checkpoint_resumes_through_stable_budget_ledger_id(tmp_path) -> None:
    registry, calls = _probe_registry()
    store = CheckpointStore(tmp_path / "replan-ledger.sqlite")
    try:
        turns = [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "probe", "args": {}, "id": f"replan-c{i}", "type": "tool_call"}
                ],
            )
            for i in range(2)
        ]
        crashing = ReActAgent(
            model=_CrashAfterScript(messages=iter(turns)),
            registry=registry,
            max_steps=2,
            max_model_calls=10,
            max_tool_calls=10,
            max_replans=1,
            checkpointer=store.saver,
        )
        with pytest.raises(RuntimeError, match="crash-mid-run"):
            crashing.run("replan then crash", thread_id="replan-first")
        final_attempt_thread = crashing.last_thread_id
        assert final_attempt_thread and final_attempt_thread != "replan-first"
        values = store.saver.get(
            {"configurable": {"thread_id": final_attempt_thread}}
        )["channel_values"]
        assert values["owc_run_contract"]["budget_ledger_id"] == "replan-first"

        resumed = ReActAgent(
            model=scripted_chat_model(["done after retry"]),
            registry=registry,
            checkpointer=store.saver,
        ).run(thread_id=final_attempt_thread, resume=True)
    finally:
        store.close()

    assert calls == ["probe", "probe"]
    assert resumed.final_answer == "done after retry"


def _restored_model_budget(
    store: CheckpointStore, thread_id: str, registry: SkillRegistry
):
    restored = ReActAgent(
        model=scripted_chat_model(["unused"]),
        registry=registry,
        checkpointer=store.saver,
    )
    restored._activate_run_contract(resume=True, thread_id=thread_id)
    assert restored._active_total_model_budget is not None
    return restored._active_total_model_budget


def test_compression_dispatch_crash_is_durably_reserved_before_resume(tmp_path) -> None:
    registry, _ = _probe_registry()
    turns = [
        AIMessage(
            content="",
            tool_calls=[
                {"name": "probe", "args": {}, "id": f"compact-c{i}", "type": "tool_call"}
            ],
        )
        for i in range(2)
    ]
    store = CheckpointStore(tmp_path / "compact-crash.sqlite")
    try:
        with pytest.raises(_FatalDispatchCrash, match="transcript compressor"):
            ReActAgent(
                model=_CrashOnAuxiliaryCall(
                    messages=iter(turns), crash_marker="transcript compressor"
                ),
                registry=registry,
                max_steps=6,
                max_model_calls=10,
                transcript_token_budget=10,
                compression_threshold=0.1,
                compression_keep_recent=1,
                checkpointer=store.saver,
            ).run("force compaction", thread_id="compact-crash")

        channel_used = store.saver.get(
            {"configurable": {"thread_id": "compact-crash"}}
        )["channel_values"]["owc_total_model_calls"]
        ledger_used, kinds = CheckpointModelBudgetLedger(store.saver).snapshot(
            "compact-crash"
        )
        # Two completed planning calls reached the graph channel. The crashing compression call
        # made one durable pre-dispatch reservation; planning is now reserved in its wrapper (after
        # compaction), so there is no phantom reservation for a planning call never dispatched.
        assert channel_used == 2
        assert ledger_used == 3
        assert kinds["compression"] == 1
        restored = _restored_model_budget(store, "compact-crash", registry)
    finally:
        store.close()

    assert restored.used == 3


def test_generate_dispatch_crash_is_durably_reserved_before_resume(tmp_path) -> None:
    registry, _ = _probe_registry()
    turn = AIMessage(
        content="",
        tool_calls=[
            {"name": "probe", "args": {}, "id": "generate-c0", "type": "tool_call"}
        ],
    )
    store = CheckpointStore(tmp_path / "generate-crash.sqlite")
    try:
        with pytest.raises(_FatalDispatchCrash, match="tool-call step budget"):
            ReActAgent(
                model=_CrashOnAuxiliaryCall(
                    messages=iter([turn]), crash_marker="tool-call step budget"
                ),
                registry=registry,
                max_steps=1,
                max_model_calls=4,
                early_stopping="generate",
                checkpointer=store.saver,
            ).run("force generate", thread_id="generate-crash")

        channel_used = store.saver.get(
            {"configurable": {"thread_id": "generate-crash"}}
        )["channel_values"]["owc_total_model_calls"]
        ledger_used, kinds = CheckpointModelBudgetLedger(store.saver).snapshot(
            "generate-crash"
        )
        assert channel_used == 1
        assert ledger_used == 2
        assert kinds["generate"] == 1
        restored = _restored_model_budget(store, "generate-crash", registry)
    finally:
        store.close()

    assert restored.used == 2


def test_reflection_dispatch_crash_is_durably_reserved_before_resume(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("OWCOPILOT_ALLOW_OFFLINE_LLM", "0")
    registry, _ = _probe_registry()
    store = CheckpointStore(tmp_path / "reflection-crash.sqlite")
    try:
        agent = ReActAgent(
            model=_CrashOnAuxiliaryCall(
                messages=iter([AIMessage(content="initial done")]),
                crash_marker="self-reflection component",
            ),
            registry=registry,
            max_model_calls=4,
            checkpointer=store.saver,
        )
        agent.run("initial", thread_id="reflection-crash")
        with pytest.raises(_FatalDispatchCrash, match="self-reflection component"):
            distill_reflection(
                agent=agent,
                goal="initial",
                trial_index=0,
                feedback="needs another approach",
            )

        channel_used = store.saver.get(
            {"configurable": {"thread_id": "reflection-crash"}}
        )["channel_values"]["owc_total_model_calls"]
        ledger_used, kinds = CheckpointModelBudgetLedger(store.saver).snapshot(
            "reflection-crash"
        )
        assert channel_used == 1
        assert ledger_used == 2
        assert kinds["reflection"] == 1
        restored = _restored_model_budget(store, "reflection-crash", registry)
    finally:
        store.close()

    assert restored.used == 2


# --------------------------------------------------------------------------- CLI single-agent


def test_cli_agent_surfaces_thread_id_and_writes_checkpoint_file(tmp_path, capsys) -> None:
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    code = main(
        ["agent", "--content-root", str(content_root), "--goal", "ready?", "--llm-mode", "offline"]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    # Always-on checkpointer ⇒ a thread_id is minted and surfaced, and the file exists.
    assert payload["thread_id"]
    assert (content_root / ".owcopilot" / "checkpoints.sqlite").exists()
    assert payload["stop_reason"] == "finished"


def test_cli_agent_pinned_thread_id_is_surfaced(tmp_path, capsys) -> None:
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    code = main(
        [
            "agent", "--content-root", str(content_root), "--goal", "ready?",
            "--llm-mode", "offline", "--thread-id", "my-thread",
        ]
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out)["thread_id"] == "my-thread"


def test_cli_agent_reused_thread_without_resume_is_guided_error(tmp_path, capsys) -> None:
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    base = [
        "agent", "--content-root", str(content_root), "--goal", "ready?", "--llm-mode", "offline",
    ]
    assert main([*base, "--thread-id", "dup"]) == 0
    capsys.readouterr()
    # Reusing an existing thread without --resume must be refused (no silent accumulate/overwrite).
    code = main([*base, "--thread-id", "dup"])
    assert code == 2
    err = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert "resume" in err["error"].lower() or "已存在" in err["error"]


def test_cli_agent_resume_missing_thread_is_guided_error(tmp_path, capsys) -> None:
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    code = main(
        [
            "agent", "--content-root", str(content_root),
            "--llm-mode", "offline", "--thread-id", "nope", "--resume",
        ]
    )
    assert code == 2
    err = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert "checkpoint" in err["error"].lower() or "找不到" in err["error"]


# --------------------------------------------------------------------------- multi-agent (scheme Y)


def test_multi_agent_checkpointer_parity_and_wiring(tmp_path) -> None:
    from owcopilot.multi_agent import MultiAgentSession

    content_root = tmp_path / "content"
    _dirty_project(content_root)
    registry = default_skill_registry(content_root=str(content_root))
    model = OfflineReactChatModel()

    plain = MultiAgentSession(orchestrator_model=model, model=model, registry=registry)
    plain_report = plain.run("ready to export", session_id="sid-fixed")
    plain.close()

    store = CheckpointStore(tmp_path / "cp.sqlite")
    try:
        session = MultiAgentSession(
            orchestrator_model=model, model=model, registry=registry, checkpointer=store.saver
        )
        report = session.run("ready to export", session_id="sid-fixed", thread_id="t-1")
        # Wiring: the checkpointer is actually compiled onto the graph and wrote a checkpoint.
        assert session.last_thread_id == "t-1"
        assert store.thread_exists("t-1")
        session.close()
    finally:
        store.close()

    # Parity: the report is byte-identical to the no-checkpointer run.
    assert report.synthesis == plain_report.synthesis
    assert report.participants == plain_report.participants
    assert report.worker_summaries == plain_report.worker_summaries
    assert report.verifier_verdicts == plain_report.verifier_verdicts


def test_cli_multi_agent_surfaces_thread_id(tmp_path, capsys) -> None:
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    code = main(
        [
            "multi-agent", "--content-root", str(content_root),
            "--goal", "让世界可导出", "--llm-mode", "offline",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["thread_id"]
    assert (content_root / ".owcopilot" / "checkpoints.sqlite").exists()


def test_interrupt_resume_roundtrip_on_checkpoint_infra(tmp_path) -> None:
    # T6 foresight (design §1.5 / S4): the SAME SqliteSaver + thread_id infra CheckpointStore builds
    # is what an in-process HITL interrupt needs. A node ``interrupt()`` suspends cleanly; a
    # ``Command(resume=...)`` continues from the suspend point. Pins the infra contract T6 reuses.
    class _S(TypedDict):
        steps: Annotated[list[str], operator.add]
        approved: str

    def step1(_state: _S) -> dict:
        return {"steps": ["step1"]}

    def gate(_state: _S) -> dict:
        decision = interrupt({"question": "approve?"})
        return {"steps": ["gate"], "approved": decision}

    def step3(state: _S) -> dict:
        return {"steps": ["step3-after-" + state["approved"]]}

    store = CheckpointStore(tmp_path / "cp.sqlite")
    try:
        g = StateGraph(_S)
        g.add_node("step1", step1)
        g.add_node("gate", gate)
        g.add_node("step3", step3)
        g.add_edge(START, "step1")
        g.add_edge("step1", "gate")
        g.add_edge("gate", "step3")
        g.add_edge("step3", END)
        graph = g.compile(checkpointer=store.saver)

        cfg = {"configurable": {"thread_id": "hitl-1"}}
        graph.invoke({"steps": [], "approved": ""}, cfg)
        snapshot = graph.get_state(cfg)
        assert snapshot.next == ("gate",)  # suspended at the human gate
        assert snapshot.interrupts

        final = graph.invoke(Command(resume="YES"), cfg)
        assert final["steps"] == ["step1", "gate", "step3-after-YES"]
        assert final["approved"] == "YES"
    finally:
        store.close()


def test_module_uses_independent_checkpoints_file_type() -> None:
    # Guard: CheckpointStore.saver is a SqliteSaver (the API create_agent / compile expect).
    store = CheckpointStore(":memory:")
    try:
        assert isinstance(store.saver, SqliteSaver)
    finally:
        store.close()
