"""Episodic memory READ side (owcopilot.agent.episodic).

M1 — MemGPT recall storage: embed a run's (goal + outcome), then recall the most similar FINISHED
past runs and inject them as prompt context.
M2 — Reflexion cross-trial buffer: persist a distilled reflection, then recall it into a later
run's reflection slot.

All $0 / offline: conftest pins OWCOPILOT_EMBEDDER=hashing (deterministic) and
OWCOPILOT_ALLOW_OFFLINE_LLM=1, so nothing here touches a network or a paid model.
"""

from __future__ import annotations

import logging

from owcopilot.agent import ReActAgent
from owcopilot.agent.episodic import (
    persist_reflection,
    persist_run_embedding,
    recall_reflections,
    recall_similar_runs,
)
from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest
from owcopilot.content.store import ContentStore
from owcopilot.llm.cache import HashingEmbedder, NoOpCache
from owcopilot.llm.gateway import LLMGateway
from owcopilot.llm.router import StaticRouter
from owcopilot.llm.telemetry import TelemetryCollector
from owcopilot.skills import SkillRegistry
from owcopilot.storage.sqlite import SQLiteStore

GOAL = "Get this world ready to export."


# --------------------------------------------------------------------------- helpers


def _dirty_project(content_root) -> None:
    ContentStore(content_root).save(
        ContentBundle(
            entities={
                "npc_aldric": Entity(
                    id="npc_aldric",
                    name="Aldric",
                    type=EntityType.NPC,
                    description="Caravan master",
                )
            },
            quests={"q1": Quest(id="q1", title="Q1", giver_npc="npc_missing")},
        )
    )


def _gateway(provider) -> LLMGateway:
    return LLMGateway(
        providers={"cheap": provider},
        router=StaticRouter(mapping={"agent_react": "cheap"}),
        cache=NoOpCache(),
    )


class _RecordingProvider:
    """Records the exact ``user`` prompt of every planning call so a test can assert what context
    (e.g. recalled reflections) the actor was actually handed."""

    def __init__(self, reply: str = "Thought: done.\nFinal Answer: ok") -> None:
        self.reply = reply
        self.seen_user: list[str] = []

    def complete(self, *, system: str, user: str, model: str):
        self.seen_user.append(user)
        return self.reply, 1, 1


def _store_run(
    store: SQLiteStore,
    embedder,
    *,
    run_id: str,
    goal: str,
    final_answer: str = "done",
    stop_reason: str = "finished",
) -> None:
    """Persist a run row (so the recall join sees it) plus its recall embedding."""
    store.save_agent_run(
        run_id=run_id,
        goal=goal,
        stop_reason=stop_reason,
        step_count=1,
        cost_usd=0.0,
        latency_ms=0.0,
        agent_name="world-consistency",
        transcript_json="{}",
    )
    persist_run_embedding(store, embedder, run_id=run_id, goal=goal, final_answer=final_answer)


# =========================================================================== M1: recall runs


def test_recall_hits_same_goal() -> None:
    """① persist then recall the same goal → the run recalls itself with near-1.0 similarity."""
    store = SQLiteStore(":memory:")
    embedder = HashingEmbedder()
    goal = "check quest giver consistency for the Aldric caravan"
    _store_run(store, embedder, run_id="r1", goal=goal, final_answer="Found a missing giver npc.")

    hits = recall_similar_runs(store, embedder, goal)

    assert [h.run_id for h in hits] == ["r1"]
    # The stored vector embeds goal + outcome summary while the query embeds the goal alone, so a
    # self-hit is strong but not exactly 1.0; comfortably above the 0.35 recall threshold.
    assert hits[0].similarity > 0.5
    assert hits[0].goal == goal
    assert hits[0].summary == "Found a missing giver npc."


def test_recall_unrelated_goal_below_threshold() -> None:
    """② an unrelated goal (no shared tokens) falls below min_similarity → no hit."""
    store = SQLiteStore(":memory:")
    embedder = HashingEmbedder()
    _store_run(store, embedder, run_id="r1", goal="escort caravan mountain pass bandits ambush")

    hits = recall_similar_runs(store, embedder, "bake chocolate strawberry dessert recipe")

    assert hits == []


def test_recall_k_truncates_to_top_matches() -> None:
    """③ k caps the returned hits, ordered by descending similarity."""
    store = SQLiteStore(":memory:")
    embedder = HashingEmbedder()
    for i in range(4):
        _store_run(
            store, embedder, run_id=f"r{i}", goal=f"audit quest consistency world region {i}"
        )

    hits = recall_similar_runs(store, embedder, "audit quest consistency world region", k=2)

    assert len(hits) == 2
    assert hits[0].similarity >= hits[1].similarity


def test_recall_skips_rows_from_a_different_embedding_space() -> None:
    """④ a row written under a different model_id is not comparable and is skipped."""
    store = SQLiteStore(":memory:")
    embedder = HashingEmbedder()  # model_id == "hashing-1024"
    goal = "audit quest consistency for the caravan"
    # A matching-space row (recallable) …
    _store_run(store, embedder, run_id="match", goal=goal)
    # … and a finished run whose embedding was written under ANOTHER embedder id (not comparable).
    store.save_agent_run(
        run_id="other",
        goal=goal,
        stop_reason="finished",
        step_count=1,
        cost_usd=0.0,
        latency_ms=0.0,
        agent_name="world-consistency",
        transcript_json="{}",
    )
    store.save_agent_run_embedding(
        run_id="other",
        model_id="some-other-model",
        goal=goal,
        summary="done",
        vector=b"\x00\x00\x80?" * 8,  # 8 float32 ones; wrong space AND wrong dim
    )

    hits = recall_similar_runs(store, embedder, goal)

    assert [h.run_id for h in hits] == ["match"]


def test_recall_only_finished_runs() -> None:
    """A max_steps run carries no trustworthy conclusion → its embedding is never recalled."""
    store = SQLiteStore(":memory:")
    embedder = HashingEmbedder()
    goal = "audit quest consistency for the caravan"
    _store_run(store, embedder, run_id="unfinished", goal=goal, stop_reason="max_steps")

    assert recall_similar_runs(store, embedder, goal) == []

    # The same run, once finished, IS recalled — confirming it was the stop_reason filter, not
    # a missing embedding, that excluded it above.
    _store_run(store, embedder, run_id="done", goal=goal, stop_reason="finished")
    assert [h.run_id for h in recall_similar_runs(store, embedder, goal)] == ["done"]


def test_persist_run_embedding_fails_open_on_embed_error(caplog) -> None:
    """⑥ an embed failure does not block the run: it stays stored, no embedding, warning logged."""

    class _RaisingEmbedder:
        model_id = "raising"

        def embed(self, text: str) -> list[float]:
            raise RuntimeError("boom")

        def embed_many(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("boom")

    store = SQLiteStore(":memory:")
    store.save_agent_run(
        run_id="r1",
        goal="g",
        stop_reason="finished",
        step_count=1,
        cost_usd=0.0,
        latency_ms=0.0,
        agent_name="world-consistency",
        transcript_json="{}",
    )

    with caplog.at_level(logging.WARNING, logger="owcopilot.agent.episodic"):
        persist_run_embedding(store, _RaisingEmbedder(), run_id="r1", goal="g", final_answer="a")

    # The run itself is untouched, and no embedding was written (nothing to recall in that space).
    assert store.get_agent_run("r1") is not None
    assert store.query_agent_run_embeddings(model_id="raising") == []
    assert any(rec.levelno == logging.WARNING for rec in caplog.records)


# =========================================================================== M1: CLI wiring


def test_cli_no_recall_skips_recall(tmp_path, monkeypatch, capsys) -> None:
    """⑤ --no-recall must NOT call the recall functions; the default run must call them."""
    from owcopilot.agent import episodic as episodic_mod
    from owcopilot.cli.main import main

    content_root = tmp_path / "content"
    _dirty_project(content_root)

    calls = {"n": 0}

    def _spy(*_args, **_kwargs):
        calls["n"] += 1
        return []

    monkeypatch.setattr(episodic_mod, "recall_similar_runs", _spy)
    monkeypatch.setattr(episodic_mod, "recall_reflections", _spy)

    # --no-recall: the read side is off, so neither recall function is invoked.
    code = main(
        [
            "agent",
            "--content-root",
            str(content_root),
            "--goal",
            GOAL,
            "--llm-mode",
            "offline",
            "--no-recall",
        ]
    )
    assert code == 0
    capsys.readouterr()
    assert calls["n"] == 0

    # Default (recall on): recall IS invoked.
    code = main(
        ["agent", "--content-root", str(content_root), "--goal", GOAL, "--llm-mode", "offline"]
    )
    assert code == 0
    capsys.readouterr()
    assert calls["n"] >= 1


def test_cli_agent_persists_recall_embedding(tmp_path, capsys) -> None:
    """The agent command writes a recall embedding (keyed to the run + its outcome) so the run is a
    future recall candidate. The recall ranking itself is exercised with controlled inputs above;
    here the point is the CLI wiring — the write side fires on a real run."""
    import json

    from owcopilot.cli.main import main

    content_root = tmp_path / "content"
    _dirty_project(content_root)

    code = main(
        ["agent", "--content-root", str(content_root), "--goal", GOAL, "--llm-mode", "offline"]
    )
    assert code == 0
    run_id = json.loads(capsys.readouterr().out)["agent_run_id"]

    # Re-open the same runtime store (the CLI default path) and confirm the embedding was persisted
    # under the run's id and embedder space, carrying the run's goal and an outcome summary.
    store = SQLiteStore(str(content_root / ".owcopilot" / "runtime.sqlite"))
    try:
        rows = store.query_agent_run_embeddings(model_id=HashingEmbedder().model_id)
        assert [r["run_id"] for r in rows] == [run_id]
        assert rows[0]["goal"] == GOAL
        assert rows[0]["summary"]  # the outcome summary was captured
    finally:
        store.close()


# =========================================================================== M2: reflections


def test_reflection_persist_and_recall_into_prompt() -> None:
    """② a similar goal recalls the reflection, which lands in the actor's prompt (fake gateway)."""
    store = SQLiteStore(":memory:")
    embedder = HashingEmbedder()
    lesson = "always run audit_project before concluding the world is clean"
    persist_reflection(
        store,
        embedder,
        goal="audit quest giver consistency for the caravan",
        reflection=lesson,
        source="multi_agent_retry",
    )

    hits = recall_reflections(
        store, embedder, "audit quest giver consistency for the caravan quest"
    )
    assert [h.reflection for h in hits] == [lesson]
    assert hits[0].source == "multi_agent_retry"

    # The recalled reflection reaches the actor through ReActAgent's existing reflections channel.
    provider = _RecordingProvider()
    ReActAgent(gateway=_gateway(provider), registry=SkillRegistry(), max_steps=1).run(
        "audit quest giver consistency for the caravan quest",
        reflections=[h.reflection for h in hits],
    )
    assert "Reflections from previous attempts:" in provider.seen_user[0]
    assert lesson in provider.seen_user[0]


def test_reflection_not_recalled_for_dissimilar_goal() -> None:
    """③ a dissimilar goal recalls nothing → no reflection is injected."""
    store = SQLiteStore(":memory:")
    embedder = HashingEmbedder()
    persist_reflection(
        store,
        embedder,
        goal="audit quest giver consistency for the caravan",
        reflection="always run audit_project first",
        source="multi_agent_retry",
    )

    hits = recall_reflections(store, embedder, "bake chocolate strawberry cake dessert")

    assert hits == []


def test_multi_agent_retry_persists_reflection_offline() -> None:
    """① a verifier 'fail' drives a Reflexion retry whose distilled reflection is PERSISTED; the
    whole chain is $0 / offline (hashing embedder, offline reflection template)."""
    from owcopilot.multi_agent import MultiAgentSession
    from owcopilot.skills import (
        CostTier,
        SideEffect,
        Skill,
        SkillParameter,
    )
    from owcopilot.skills import (
        SkillRegistry as _Registry,
    )

    _DECOMPOSE_JSON = (
        '[{"role": "diagnosis", "description": "Audit and report open errors", '
        '"allowed_skills": ["audit_project", "list_issues"], "worker_id": "diag_01"}, '
        '{"role": "repair_proposal", "description": "Propose fixes", '
        '"allowed_skills": ["propose_fix"], "worker_id": "repair_01"}]'
    )

    def _make_audit_registry(open_errors: int):
        reg = _Registry()
        reg.register(
            Skill(
                name="audit_project",
                description="Deterministic audit (fake).",
                cost_tier=CostTier.DETERMINISTIC,
                side_effect=SideEffect.READ_ONLY,
                handler=lambda **_k: {"open_errors": open_errors, "issues": []},
            )
        )
        reg.register(
            Skill(
                name="list_issues",
                description="List issues (fake).",
                cost_tier=CostTier.DETERMINISTIC,
                side_effect=SideEffect.READ_ONLY,
                handler=lambda **_k: {"count": open_errors, "issues": []},
            )
        )
        reg.register(
            Skill(
                name="propose_fix",
                description="Propose a fix (fake).",
                cost_tier=CostTier.DETERMINISTIC,
                side_effect=SideEffect.PROPOSES_PATCH,
                handler=lambda *, issue_id, **_k: {"issue_id": issue_id, "proposed": True},
                parameters=(SkillParameter("issue_id", "string", "issue id", required=True),),
            )
        )
        return reg

    class _LazyThenDiligentProvider:
        def complete(self, *, system: str, user: str, model: str):
            if "task-decomposition" in system or "Decompose into subtasks" in user:
                return _DECOMPOSE_JSON, 10, 10
            if "[Your specific subtask as diagnosis]" in user:
                if "Reflections from previous attempts:" not in user:
                    return (
                        (
                            "Thought: looks fine to me.\n"
                            "Final Answer: The world is clean. I found 0 open errors."
                        ),
                        5,
                        5,
                    )
                if "Observation:" not in user:
                    return (
                        (
                            "Thought: audit first this time.\n"
                            "Action: audit_project\nAction Input: {}"
                        ),
                        5,
                        5,
                    )
                return "Thought: now I know.\nFinal Answer: 一致性审计发现 3 个待修复错误。", 5, 5
            return "Thought: done.\nFinal Answer: 已生成修复提案，等待人工审阅。", 5, 2

    registry = _make_audit_registry(open_errors=3)
    provider = _LazyThenDiligentProvider()
    telemetry = TelemetryCollector()
    gw = LLMGateway({"cheap": provider, "frontier": provider}, telemetry=telemetry)
    store = SQLiteStore(":memory:")
    embedder = HashingEmbedder()

    session = MultiAgentSession(gateway=gw, registry=registry, store=store, embedder=embedder)
    session.run("diagnose and fix")

    # The distilled reflection was persisted to the cross-trial buffer, keyed and embedded.
    rows = store.query_reflections(model_id=embedder.model_id)
    assert len(rows) == 1
    assert rows[0]["source"] == "multi_agent_retry"
    assert rows[0]["reflection"]

    # ④ Offline $0 chain: the vector was produced by the deterministic hashing stub (no paid
    # embedding API — the query filter above already binds model_id to it) and the reflection came
    # from the offline Self-Reflection template (no LLM call), not a live model.
    assert embedder.model_id.startswith("hashing")
    assert rows[0]["reflection"].startswith("第1次尝试未通过")
