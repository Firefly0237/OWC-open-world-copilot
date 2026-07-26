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
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from typing_extensions import TypedDict

from owcopilot.agent import ReActAgent
from owcopilot.agent.offline import OfflineReactChatModel
from owcopilot.cli.main import main
from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest
from owcopilot.content.store import ContentStore
from owcopilot.fakes import scripted_chat_model
from owcopilot.skills import CostTier, SideEffect, Skill, SkillRegistry, default_skill_registry
from owcopilot.storage.checkpointer import CheckpointStore, resolve_thread_id

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
