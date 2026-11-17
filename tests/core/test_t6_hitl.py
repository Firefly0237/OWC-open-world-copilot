"""T6 HITL 双形态验收测试（形态 a 内联 interrupt + 形态 b 异步评审队列）.

Covers, all $0 offline against LangChain-standard doubles + the REAL production code
(``ReActAgent`` / ``CheckpointStore`` / the CLI ``agent``/``draft``/``review`` commands /
``decide_review_item``):

Form a (inline interrupt, reuses the T4 checkpointer base — NOT a re-built mechanism):
* the run PAUSES before a sensitive (PROPOSES_PATCH) tool call and surfaces the pending HITL gate,
  and the tool does NOT run until a human decides;
* **approve** resumes and runs the tool; **reject** skips it and feeds the model a rejection
  observation, then the run finishes;
* a checkpointer is required (interrupt/resume needs persisted state — fails loudly otherwise);
* HITL disabled — and enabled-but-not-triggered — leave the ``AgentResult`` byte-identical (A2);
* an interrupted attempt never triggers T5 replanning (a pause is a wait for a human, not failure);
* CLI: ``agent --hitl`` pauses (``status=interrupted`` + thread_id); ``--resume --decision`` cont.

Form b (async review queue, reuses the EXISTING draft→review→accept path — NOT re-built):
* an AI draft submitted to the queue is async-accepted into canon via the real CLI ``review``
  command (the only AI-content write path).
"""

from __future__ import annotations

import json

import pytest

from owcopilot.agent import ReActAgent
from owcopilot.content.models import ContentBundle, Entity, EntityType
from owcopilot.content.store import ContentStore
from owcopilot.fakes import scripted_chat_model
from owcopilot.skills import CostTier, SideEffect, Skill, SkillRegistry
from owcopilot.storage.checkpointer import CheckpointStore

# --------------------------------------------------------------------------- doubles / helpers

_PROPOSE_TOOL = "propose_fix"


def _propose_registry() -> tuple[SkillRegistry, list[dict]]:
    """A registry with a read-only ``audit_project`` and a PROPOSES_PATCH ``propose_fix`` skill.

    ``propose_fix`` records each invocation into the returned list so a test can assert the tool
    ran (approve) or did not (interrupt pending / reject) — the ground truth of the HITL gate.
    """
    calls: list[dict] = []
    registry = SkillRegistry()
    registry.register(
        Skill(
            name="audit_project",
            description="Audit the world (read-only, deterministic).",
            cost_tier=CostTier.DETERMINISTIC,
            side_effect=SideEffect.READ_ONLY,
            handler=lambda **_kw: {"open_errors": 1},
            parameters=(),
        )
    )
    registry.register(
        Skill(
            name=_PROPOSE_TOOL,
            description="Propose a shadow-validated fix (PROPOSES_PATCH; never writes canon).",
            cost_tier=CostTier.DETERMINISTIC,
            side_effect=SideEffect.PROPOSES_PATCH,
            handler=lambda **kw: (calls.append(kw), {"ok": True, "candidates": 2})[1],
            parameters=(),
        )
    )
    return registry, calls


def _seed_project(content_root) -> None:
    # A CLEAN project (no dangling refs) so the form-b post-accept audit reflects only the accepted
    # draft — a pre-seeded broken quest would show as pre-existing open errors and mask the gate.
    ContentStore(content_root).save(
        ContentBundle(
            entities={
                "npc_aldric": Entity(
                    id="npc_aldric", name="Aldric", type=EntityType.NPC, description="Caravan boss"
                )
            },
        )
    )


# ================================================== Form a — inline interrupt (ReActAgent)


def test_hitl_pauses_before_sensitive_tool_and_surfaces_gate(tmp_path) -> None:
    # A1 core: a sensitive tool call PAUSES the run; the pending HITL gate is surfaced and the tool
    # has NOT executed (calls empty). Reuses the T4 CheckpointStore/thread infra, not a re-build.
    store = CheckpointStore(tmp_path / "cp.sqlite")
    registry, calls = _propose_registry()
    try:
        result = ReActAgent(
            model=scripted_chat_model([(_PROPOSE_TOOL, {"issue_id": "iss-1"}), "reported"]),
            registry=registry,
            checkpointer=store.saver,
            interrupt_before={_PROPOSE_TOOL},
        ).run("propose a fix", thread_id="hitl-1")
    finally:
        store.close()

    assert result.interrupted is True
    assert result.stop_reason == "interrupted"
    assert result.final_answer == ""  # no answer yet — awaiting the human
    assert calls == []  # the tool did NOT run before approval
    action = result.interrupt_request["action_requests"][0]  # type: ignore[index]
    assert action["name"] == _PROPOSE_TOOL
    review = result.interrupt_request["review_configs"][0]  # type: ignore[index]
    assert "approve" in review["allowed_decisions"] and "reject" in review["allowed_decisions"]


def test_hitl_approve_resumes_and_runs_the_tool(tmp_path) -> None:
    # approve: resume via resume_decisions (Command(resume=...)) — the gated tool now RUNS and the
    # run finishes. The resume rebuilds a fresh graph on the SAME checkpointer+thread (as the CLI).
    store = CheckpointStore(tmp_path / "cp.sqlite")
    registry, calls = _propose_registry()
    try:
        paused = ReActAgent(
            model=scripted_chat_model([(_PROPOSE_TOOL, {"issue_id": "iss-1"}), "x"]),
            registry=registry,
            checkpointer=store.saver,
            interrupt_before={_PROPOSE_TOOL},
        ).run("propose a fix", thread_id="ap-1")
        assert paused.interrupted and calls == []

        resumed = ReActAgent(
            model=scripted_chat_model(["fix proposed; see the queue"]),
            registry=registry,
            checkpointer=store.saver,
            interrupt_before={_PROPOSE_TOOL},
        ).run(thread_id="ap-1", resume=True, resume_decisions=[{"type": "approve"}])
    finally:
        store.close()

    assert resumed.interrupted is False
    assert resumed.stop_reason == "finished"
    assert calls == [{}]  # the tool ran exactly once, AFTER approval
    assert resumed.final_answer == "fix proposed; see the queue"
    assert [step.action for step in resumed.steps] == [_PROPOSE_TOOL]
    assert resumed.steps[0].is_error is False


def test_hitl_reject_skips_the_tool_and_feeds_back_a_rejection(tmp_path) -> None:
    # reject: resume with a reject decision — the tool is NOT executed, the model gets a rejection
    # observation (carrying the message), and the run finishes on the model's next answer.
    store = CheckpointStore(tmp_path / "cp.sqlite")
    registry, calls = _propose_registry()
    try:
        ReActAgent(
            model=scripted_chat_model([(_PROPOSE_TOOL, {"issue_id": "iss-1"}), "x"]),
            registry=registry,
            checkpointer=store.saver,
            interrupt_before={_PROPOSE_TOOL},
        ).run("propose a fix", thread_id="rj-1")

        resumed = ReActAgent(
            model=scripted_chat_model(["understood, standing down"]),
            registry=registry,
            checkpointer=store.saver,
            interrupt_before={_PROPOSE_TOOL},
        ).run(
            thread_id="rj-1",
            resume=True,
            resume_decisions=[{"type": "reject", "message": "unsafe for canon"}],
        )
    finally:
        store.close()

    assert resumed.interrupted is False
    assert resumed.stop_reason == "finished"
    assert calls == []  # rejected ⇒ the tool never ran
    assert [step.action for step in resumed.steps] == [_PROPOSE_TOOL]
    assert resumed.steps[0].is_error is True
    assert "unsafe for canon" in resumed.steps[0].observation
    assert resumed.final_answer == "understood, standing down"


def test_hitl_requires_a_checkpointer() -> None:
    # interrupt/resume needs persisted execution state — asking for HITL without a checkpointer is a
    # misuse that fails loudly (never a silent no-op).
    registry, _ = _propose_registry()
    with pytest.raises(ValueError, match="checkpointer"):
        ReActAgent(
            model=scripted_chat_model(["x"]),
            registry=registry,
            interrupt_before={_PROPOSE_TOOL},
        )


def test_hitl_disabled_and_dormant_are_byte_identical(tmp_path) -> None:
    # A2: HITL off (interrupt_before=None) AND HITL on-but-not-triggered (the gate is armed for
    # propose_fix but the model only calls audit_project) both produce a byte-identical AgentResult
    # vs a plain run — the middleware is inert unless a gated tool is actually proposed.
    store = CheckpointStore(":memory:")
    try:
        reg_a, _ = _propose_registry()
        plain = ReActAgent(
            model=scripted_chat_model([("audit_project", {}), "done"]),
            registry=reg_a,
            checkpointer=store.saver,
        ).run("check", thread_id="plain-1")

        reg_b, _ = _propose_registry()
        dormant = ReActAgent(
            model=scripted_chat_model([("audit_project", {}), "done"]),
            registry=reg_b,
            checkpointer=store.saver,
            interrupt_before={_PROPOSE_TOOL},
        ).run("check", thread_id="dormant-1")
    finally:
        store.close()

    assert plain.interrupted is False
    assert dormant.model_dump(mode="json") == plain.model_dump(mode="json")


def test_hitl_interrupt_does_not_trigger_t5_replanning(tmp_path) -> None:
    # T5 interaction: a paused attempt is a WAIT for a human, not a failed attempt — so it must
    # never be replanned over. With max_replans=2 an interrupted first attempt returns immediately
    # (replans=0, interrupted=True) instead of distilling a reflection and re-running.
    store = CheckpointStore(tmp_path / "cp.sqlite")
    registry, calls = _propose_registry()
    try:
        result = ReActAgent(
            model=scripted_chat_model([(_PROPOSE_TOOL, {"issue_id": "iss-1"}), "x"]),
            registry=registry,
            checkpointer=store.saver,
            interrupt_before={_PROPOSE_TOOL},
            max_replans=2,
        ).run("propose a fix", thread_id="rp-1")
    finally:
        store.close()

    assert result.interrupted is True
    assert result.replans == 0  # never replanned over the pause
    assert calls == []


# ============================================================ Form a — inline interrupt (CLI)


def test_cli_agent_hitl_pauses_then_resume_approve(tmp_path, capsys, monkeypatch) -> None:
    # A1 CLI use case (form a): `agent --hitl` pauses at the sensitive tool (status=interrupted +
    # surfaced gate + thread_id), and `--resume --decision approve` continues to completion.
    import owcopilot.cli.main as cli_main

    content_root = tmp_path / "content"
    _seed_project(content_root)

    def fake_model(args):
        # A fresh model per CLI invocation; the resume call only needs the final-answer turn.
        if getattr(args, "resume", False):
            return scripted_chat_model(["fix proposed and reported"])
        return scripted_chat_model([(_PROPOSE_TOOL, {"issue_id": "iss-x"}), "x"])

    monkeypatch.setattr(cli_main, "_agent_chat_model", fake_model)

    code = cli_main.main(
        [
            "agent", "--content-root", str(content_root), "--goal", "propose a fix",
            "--llm-mode", "offline", "--hitl", "--thread-id", "hitl-cli-1",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["status"] == "interrupted"
    assert payload["interrupted"] is True
    assert payload["thread_id"] == "hitl-cli-1"
    assert payload["interrupt_request"]["action_requests"][0]["name"] == _PROPOSE_TOOL
    assert "resume" in payload["next_step"].lower()

    code = cli_main.main(
        [
            "agent", "--content-root", str(content_root), "--llm-mode", "offline",
            "--thread-id", "hitl-cli-1", "--resume", "--decision", "approve",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["status"] == "completed"
    assert payload["interrupted"] is False
    assert payload["stop_reason"] == "finished"
    assert _PROPOSE_TOOL in [step["action"] for step in payload["steps"]]


def test_cli_agent_hitl_resume_reject(tmp_path, capsys, monkeypatch) -> None:
    # A1 CLI use case (form a): `--resume --decision reject` skips the proposed tool and records the
    # rejection message as an error observation; the run still completes.
    import owcopilot.cli.main as cli_main

    content_root = tmp_path / "content"
    _seed_project(content_root)

    def fake_model(args):
        if getattr(args, "resume", False):
            return scripted_chat_model(["standing down"])
        return scripted_chat_model([(_PROPOSE_TOOL, {"issue_id": "iss-x"}), "x"])

    monkeypatch.setattr(cli_main, "_agent_chat_model", fake_model)

    assert cli_main.main(
        [
            "agent", "--content-root", str(content_root), "--goal", "propose a fix",
            "--llm-mode", "offline", "--hitl", "--thread-id", "reject-cli-1",
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["status"] == "interrupted"

    assert cli_main.main(
        [
            "agent", "--content-root", str(content_root), "--llm-mode", "offline",
            "--thread-id", "reject-cli-1", "--resume", "--decision", "reject",
            "--decision-message", "not safe to propose",
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["status"] == "completed"
    step = next(s for s in payload["steps"] if s["action"] == _PROPOSE_TOOL)
    assert step["is_error"] is True
    assert "not safe to propose" in step["observation"]


# ============================================================ Form b — async review queue (CLI)


def test_cli_form_b_review_queue_async_accept(tmp_path, capsys) -> None:
    # A1 (form b): the EXISTING draft→review→accept path is the async HITL gate. An AI draft is
    # submitted to the queue (offline `draft`), a LATER invocation lists it pending, and a human
    # `review --accept` writes it to canon (the only AI-content write path; via decide_review_item).
    content_root = tmp_path / "content"
    _seed_project(content_root)

    cli_draft = _run_cli(
        capsys,
        ["draft", "--content-root", str(content_root), "--brief",
         "A courier task for Aldric", "--llm-mode", "offline"],
    )
    item_id = cli_draft["review_item_id"]
    assert cli_draft["review_status"] == "pending_review"

    listed = _run_cli(capsys, ["review", "--content-root", str(content_root)])
    assert listed["count"] == 1
    assert listed["items"][0]["id"] == item_id  # sits in the queue for async review

    accepted = _run_cli(
        capsys,
        ["review", "--content-root", str(content_root), "--accept", item_id, "--operator", "alice"],
    )
    assert accepted["decision"] == "accepted"
    assert accepted["written_ref"].startswith("quest:")  # written to canon
    assert accepted["post_audit_open_errors"] == 0


def _run_cli(capsys, argv: list[str]) -> dict:
    from owcopilot.cli.main import main

    assert main(argv) == 0
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])
