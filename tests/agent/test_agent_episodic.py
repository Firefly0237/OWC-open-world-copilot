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
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from owcopilot.agent import AgentResult, AgentStep, ReActAgent, persist_agent_run
from owcopilot.agent.episodic import (
    _run_importance,
    _to_blob,
    persist_reflection,
    persist_run_embedding,
    recall_reflections,
    recall_similar_runs,
    run_audit_signal,
)
from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest
from owcopilot.content.store import ContentStore
from owcopilot.llm.cache import HashingEmbedder
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


class _RecordingChatModel(BaseChatModel):
    """Chat double replying with a fixed final answer and recording every message list, so a test
    can assert what context (e.g. recalled reflections in the system suffix) the actor was
    actually handed."""

    reply: str = "ok"
    seen: list[list[BaseMessage]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "recording-chat-fake"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Any:
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(list(messages))
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.reply))])


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

    # The recalled reflection reaches the actor through ReActAgent's existing reflections channel
    # (injected as the system-message suffix by MemoryInjectionMiddleware).
    model = _RecordingChatModel()
    ReActAgent(model=model, registry=SkillRegistry(), max_steps=1).run(
        "audit quest giver consistency for the caravan quest",
        reflections=[h.reflection for h in hits],
    )
    system_text = str(model.seen[0][0].text)
    assert "Reflections from previous attempts:" in system_text
    assert lesson in system_text


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


class _RoutedChatModel(BaseChatModel):
    """Message-routed chat double: ``route(messages)`` decides each reply (goal / system-suffix /
    ToolMessage aware) — the chat-model generation of the retired prompt-routed text providers."""

    route: Any

    @property
    def _llm_type(self) -> str:
        return "routed-chat-fake"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Any:
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=self.route(messages))])


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

    class _DecomposeOnlyModel(BaseChatModel):
        """Chat model for the orchestrator's decompose calls (its own model, decompose-only)."""

        @property
        def _llm_type(self) -> str:
            return "decompose-only-fake"

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            return ChatResult(
                generations=[ChatGeneration(message=AIMessage(content=_DECOMPOSE_JSON))]
            )

    def _lazy_then_diligent(messages) -> AIMessage:
        system = str(messages[0].text) if isinstance(messages[0], SystemMessage) else ""
        human = next((str(m.text) for m in messages if isinstance(m, HumanMessage)), "")
        observations = sum(1 for m in messages if isinstance(m, ToolMessage))
        if "[Your specific subtask as diagnosis]" in human:
            if "Reflections from previous attempts:" not in system:
                # Trial 0: lazy — never audits, asserts clean → verifier fail (ground truth 3).
                return AIMessage(content="The world is clean. I found 0 open errors.")
            if observations == 0:
                return AIMessage(
                    content="audit first this time.",
                    tool_calls=[
                        {"name": "audit_project", "args": {}, "id": "call_a0", "type": "tool_call"}
                    ],
                )
            return AIMessage(content="一致性审计发现 3 个待修复错误。")
        return AIMessage(content="已生成修复提案，等待人工审阅。")

    registry = _make_audit_registry(open_errors=3)
    store = SQLiteStore(":memory:")
    embedder = HashingEmbedder()

    session = MultiAgentSession(
        orchestrator_model=_DecomposeOnlyModel(),
        model=_RoutedChatModel(route=_lazy_then_diligent),
        registry=registry,
        store=store,
        embedder=embedder,
    )
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


# ===================================================================== T7: memory lifecycle
# The three-factor recall (relevance × recency × importance), dedup, and capacity soft-delete must
# be GENUINELY in the recall/persist path — not a scoring function nobody calls. These tests drive
# the public recall + persist functions with controlled fixtures and assert the ordering, dedup and
# eviction that the lifecycle produces.


class _DictEmbedder:
    """Embedder whose vectors are looked up from a fixed table, so a test controls the query vector
    exactly (the HashingEmbedder's token quirks are irrelevant to a factor-isolation test). Stored
    candidate vectors are written to the DB directly, so relevance / dedup geometry is pinned."""

    model_id = "dict-3"

    def __init__(self, table: dict[str, list[float]]) -> None:
        self._table = table

    def embed(self, text: str) -> list[float]:
        return list(self._table[text])

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]


def _store_vec_run(
    store: SQLiteStore,
    *,
    run_id: str,
    vector: list[float],
    model_id: str = "dict-3",
    goal: str = "g",
    summary: str = "done",
    step_count: int = 5,
    created_at: datetime,
    stop_reason: str = "finished",
) -> None:
    """Persist a finished run + a hand-crafted recall embedding with a controlled created_at.

    Bypasses the embedder for the STORED vector (written verbatim) so a factor-isolation test pins
    relevance and dedup geometry; created_at / step_count drive recency and importance."""
    iso = created_at.isoformat()
    store.save_agent_run(
        run_id=run_id,
        goal=goal,
        stop_reason=stop_reason,
        step_count=step_count,
        cost_usd=0.0,
        latency_ms=0.0,
        agent_name="world-consistency",
        transcript_json="{}",
        created_at=iso,
    )
    store.save_agent_run_embedding(
        run_id=run_id,
        model_id=model_id,
        goal=goal,
        summary=summary,
        vector=_to_blob(vector),
        created_at=iso,
    )


def test_recall_recency_orders_newer_first_when_relevance_equal() -> None:
    """Two equally-relevant, equally-important runs → the NEWER one ranks first (recency factor)."""
    store = SQLiteStore(":memory:")
    # q=[1,0,0]; A=[1,1,0], B=[1,0,1] → both cos .707 to q (equal relevance), mutual .5 (no dedup).
    embedder = _DictEmbedder({"Q": [1.0, 0.0, 0.0]})
    now = datetime.now(UTC)
    _store_vec_run(store, run_id="old", vector=[1.0, 1.0, 0.0], created_at=now - timedelta(days=30))
    _store_vec_run(store, run_id="new", vector=[1.0, 0.0, 1.0], created_at=now)

    hits = recall_similar_runs(store, embedder, "Q")

    assert [h.run_id for h in hits] == ["new", "old"]
    # similarity is the raw cosine (equal here), NOT the recency-blended score used for ordering.
    assert abs(hits[0].similarity - hits[1].similarity) < 1e-4


def test_recall_importance_orders_higher_step_count_first() -> None:
    """Two equally-relevant, equally-recent runs with NO recorded audit signal → the pre-D3
    step-count proxy still decides, so the higher step_count ranks first.

    ``_store_vec_run`` writes the run row without an ``audit_signal`` (it has no ``AgentResult`` to
    read one off), which is the same state a row written before D3 is in — so this now pins the
    legacy fallback of :func:`owcopilot.agent.episodic._run_importance` rather than its main path.
    The main path, and the two biases this proxy has, are in the D3 section below."""
    store = SQLiteStore(":memory:")
    embedder = _DictEmbedder({"Q": [1.0, 0.0, 0.0]})
    now = datetime.now(UTC)
    _store_vec_run(store, run_id="shallow", vector=[1.0, 1.0, 0.0], step_count=1, created_at=now)
    _store_vec_run(store, run_id="deep", vector=[1.0, 0.0, 1.0], step_count=30, created_at=now)

    hits = recall_similar_runs(store, embedder, "Q")

    assert [h.run_id for h in hits] == ["deep", "shallow"]


def test_recall_recency_decay_outranks_a_more_relevant_but_ancient_run() -> None:
    """Recency genuinely moves ordering: a fresher, LESS-relevant run beats an ancient,
    MORE-relevant one once the old one's recency has decayed away."""
    store = SQLiteStore(":memory:")
    embedder = _DictEmbedder({"Q": [1.0, 0.0, 0.0]})
    now = datetime.now(UTC)
    # fresh, cosine 0.707; ancient, cosine 0.958 (more relevant) but ~1 year old → decayed to floor.
    _store_vec_run(store, run_id="fresh", vector=[1.0, 1.0, 0.0], created_at=now)
    _store_vec_run(
        store, run_id="ancient", vector=[1.0, 0.3, 0.0], created_at=now - timedelta(days=365)
    )

    hits = recall_similar_runs(store, embedder, "Q")

    assert [h.run_id for h in hits] == ["fresh", "ancient"]
    # The ancient hit is the MORE cosine-similar of the two — proving order flipped on recency,
    # not relevance.
    ancient = next(h for h in hits if h.run_id == "ancient")
    fresh = next(h for h in hits if h.run_id == "fresh")
    assert ancient.similarity > fresh.similarity


def test_recall_dedup_collapses_duplicate_experience() -> None:
    """Two near-identical embeddings (mutual cosine ≥ 0.95) collapse to the best-scored one."""
    store = SQLiteStore(":memory:")
    embedder = _DictEmbedder({"Q": [1.0, 0.0, 0.0]})
    now = datetime.now(UTC)
    # Identical vectors ⇒ mutual cosine 1.0 ⇒ duplicates. "new" outscores "old" on recency, so it
    # is the survivor; "old" is dropped as a duplicate rather than occupying a second recall slot.
    _store_vec_run(store, run_id="old", vector=[1.0, 1.0, 0.0], created_at=now - timedelta(days=10))
    _store_vec_run(store, run_id="new", vector=[1.0, 1.0, 0.0], created_at=now)

    hits = recall_similar_runs(store, embedder, "Q")

    assert [h.run_id for h in hits] == ["new"]


def test_recall_dedup_keeps_distinct_but_similar_episodes() -> None:
    """Distinct experiences below the dedup cutoff are BOTH kept — dedup is near-duplicate only."""
    store = SQLiteStore(":memory:")
    embedder = _DictEmbedder({"Q": [1.0, 0.0, 0.0]})
    now = datetime.now(UTC)
    # mutual cosine 0.5 (well below 0.95) → not duplicates, both surface.
    _store_vec_run(store, run_id="a", vector=[1.0, 1.0, 0.0], created_at=now)
    _store_vec_run(store, run_id="b", vector=[1.0, 0.0, 1.0], created_at=now)

    hits = recall_similar_runs(store, embedder, "Q")

    assert sorted(h.run_id for h in hits) == ["a", "b"]


def test_capacity_soft_deletes_lowest_score_and_is_recoverable() -> None:
    """Over-capacity persist soft-deletes the lowest-retention run; recall excludes it; restore
    revives it. Exercised through the real persist path (the CLI's write side), not a helper."""
    store = SQLiteStore(":memory:")
    embedder = HashingEmbedder()
    goal = "audit quest consistency for the caravan"
    # Three runs, same goal (so all recallable), differing only in step_count ⇒ importance. All
    # written "now", so recency is ~equal and importance decides retention. Capacity 2 ⇒ the lowest
    # importance (step_count=1) is evicted when the third write pushes the space over capacity.
    for run_id, steps in (("s1", 1), ("s5", 5), ("s9", 9)):
        store.save_agent_run(
            run_id=run_id,
            goal=goal,
            stop_reason="finished",
            step_count=steps,
            cost_usd=0.0,
            latency_ms=0.0,
            agent_name="world-consistency",
            transcript_json="{}",
        )
        persist_run_embedding(
            store, embedder, run_id=run_id, goal=goal, final_answer="done", capacity=2
        )

    # The lowest-importance run's embedding is tombstoned → recall no longer surfaces it.
    live = {r["run_id"] for r in store.query_agent_run_embeddings(model_id=embedder.model_id)}
    assert live == {"s5", "s9"}
    assert "s1" not in {h.run_id for h in recall_similar_runs(store, embedder, goal)}

    # Soft-delete, not physical: the agent_run row is untouched and the embedding is recoverable.
    assert store.get_agent_run("s1") is not None
    assert store.restore_agent_run_embedding("s1") is True
    live_after = {r["run_id"] for r in store.query_agent_run_embeddings(model_id=embedder.model_id)}
    assert "s1" in live_after


def test_restore_missing_embedding_returns_false() -> None:
    """Recovery path guards a bad id: restoring an unknown run reports False (guided-error hook)."""
    store = SQLiteStore(":memory:")
    assert store.restore_agent_run_embedding("nope") is False


def test_reflection_capacity_soft_deletes_oldest_and_is_recoverable() -> None:
    """Reflections carry constant importance, so capacity evicts the OLDEST live entry; recall
    excludes it and it is recoverable. Driven through the real persist_reflection write path."""
    store = SQLiteStore(":memory:")
    embedder = HashingEmbedder()
    # Two DISTINCT goals (so their vectors are not deduped) both matching the recall query. Backdate
    # the first so it is the oldest → lowest recency-retention. capacity=1 ⇒ the second write evicts
    # it. Written directly to control created_at, then capacity enforced via the public persist.
    query = "audit quest giver consistency caravan"
    old_id = "refl_old"
    store.save_reflection(
        reflection_id=old_id,
        goal=f"{query} alpha",
        reflection="stale lesson",
        source="multi_agent_retry",
        vector=_to_blob(embedder.embed(f"{query} alpha")),
        model_id=embedder.model_id,
        created_at=(datetime.now(UTC) - timedelta(days=30)).isoformat(),
    )
    persist_reflection(
        store,
        embedder,
        goal=f"{query} beta",
        reflection="fresh lesson",
        source="multi_agent_retry",
        capacity=1,
    )

    reflections = [h.reflection for h in recall_reflections(store, embedder, query)]
    assert "fresh lesson" in reflections
    assert "stale lesson" not in reflections  # oldest tombstoned by capacity

    assert store.restore_reflection(old_id) is True
    assert "stale lesson" in [h.reflection for h in recall_reflections(store, embedder, query)]


# ============================================================== D1: recency-by-last-access
# Generative Agents defines recency as decay since the memory's most recent *access*, not its
# creation. A recall write-backs `last_accessed_at` on every hit it returns, and `_recency` now
# reads that column (falling back to `created_at` for a never-recalled row).


def test_recall_touch_promotes_a_recalled_memory_over_an_equally_old_untouched_one() -> None:
    """A memory RECALLED once outranks an equally-old memory that never was, once both are equally
    relevant — the touched one's recency clock was reset to "now" by the earlier recall itself."""
    store = SQLiteStore(":memory:")
    embedder = _DictEmbedder(
        {
            # Identical to "touched"'s stored vector (cosine 1.0) vs "untouched" (cosine 0.5) —
            # with k=1 only "touched" is returned, so only it gets recency-touched.
            "TOUCH": [1.0, 1.0, 0.0],
            # Equidistant from both stored vectors (cosine 0.707 each) — relevance ties, so recency
            # is the sole remaining differentiator.
            "TIE": [1.0, 0.0, 0.0],
        }
    )
    old = datetime.now(UTC) - timedelta(days=30)
    _store_vec_run(store, run_id="touched", vector=[1.0, 1.0, 0.0], created_at=old)
    _store_vec_run(store, run_id="untouched", vector=[1.0, 0.0, 1.0], created_at=old)

    touch_hits = recall_similar_runs(store, embedder, "TOUCH", k=1)
    assert [h.run_id for h in touch_hits] == ["touched"]

    hits = recall_similar_runs(store, embedder, "TIE", k=2)

    assert [h.run_id for h in hits] == ["touched", "untouched"]


def test_recall_touch_writes_back_last_accessed_at() -> None:
    """The write-back itself: a recalled run's `last_accessed_at` moves from NULL to a real
    timestamp; an un-recalled sibling stays NULL."""
    store = SQLiteStore(":memory:")
    embedder = HashingEmbedder()
    goal = "audit quest consistency for the caravan"
    _store_run(store, embedder, run_id="hit", goal=goal)

    before = store.query_agent_run_embeddings(model_id=embedder.model_id)[0]
    assert before["last_accessed_at"] is None

    recall_similar_runs(store, embedder, goal)

    after = store.query_agent_run_embeddings(model_id=embedder.model_id)[0]
    assert after["last_accessed_at"] is not None


def test_reflection_recall_touch_promotes_a_previously_recalled_reflection() -> None:
    """Same D1 promotion, over the reflection channel."""
    store = SQLiteStore(":memory:")
    embedder = _DictEmbedder({"TOUCH": [1.0, 1.0, 0.0], "TIE": [1.0, 0.0, 0.0]})
    old_iso = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    store.save_reflection(
        reflection_id="touched",
        goal="g",
        reflection="touched lesson",
        source="multi_agent_retry",
        vector=_to_blob([1.0, 1.0, 0.0]),
        model_id="dict-3",
        created_at=old_iso,
    )
    store.save_reflection(
        reflection_id="untouched",
        goal="g",
        reflection="untouched lesson",
        source="multi_agent_retry",
        vector=_to_blob([1.0, 0.0, 1.0]),
        model_id="dict-3",
        created_at=old_iso,
    )

    touch_hits = recall_reflections(store, embedder, "TOUCH", k=1)
    assert [h.reflection for h in touch_hits] == ["touched lesson"]

    hits = recall_reflections(store, embedder, "TIE", k=2)

    assert [h.reflection for h in hits] == ["touched lesson", "untouched lesson"]


# ============================================================== D2: memory namespace (scope)
# Both tables now carry a `scope` column stamped from the store's `world_id` (the project/world
# boundary `ProjectContext` already threads through every other scoped table). Recall/capacity are
# WHERE-filtered by it, so two projects sharing one runtime.sqlite never recall each other's memory.


def test_recall_runs_isolated_by_project_scope(tmp_path) -> None:
    """Two projects (world_id) writing to the SAME runtime.sqlite each only ever recall their own
    run embeddings — never the other project's, even for the identical goal text."""
    db = str(tmp_path / "rt.db")
    store_a = SQLiteStore(db, world_id="project_a")
    store_b = SQLiteStore(db, world_id="project_b")
    embedder = HashingEmbedder()
    goal = "audit quest consistency for the caravan"
    try:
        _store_run(store_a, embedder, run_id="a1", goal=goal)
        _store_run(store_b, embedder, run_id="b1", goal=goal)

        assert [h.run_id for h in recall_similar_runs(store_a, embedder, goal)] == ["a1"]
        assert [h.run_id for h in recall_similar_runs(store_b, embedder, goal)] == ["b1"]
    finally:
        store_a.close()
        store_b.close()


def test_recall_reflections_isolated_by_project_scope(tmp_path) -> None:
    """Same D2 isolation over the reflection channel."""
    db = str(tmp_path / "rt.db")
    store_a = SQLiteStore(db, world_id="project_a")
    store_b = SQLiteStore(db, world_id="project_b")
    embedder = HashingEmbedder()
    goal = "audit quest giver consistency for the caravan"
    try:
        persist_reflection(
            store_a, embedder, goal=goal, reflection="lesson A", source="multi_agent_retry"
        )
        persist_reflection(
            store_b, embedder, goal=goal, reflection="lesson B", source="multi_agent_retry"
        )

        assert [h.reflection for h in recall_reflections(store_a, embedder, goal)] == ["lesson A"]
        assert [h.reflection for h in recall_reflections(store_b, embedder, goal)] == ["lesson B"]
    finally:
        store_a.close()
        store_b.close()


def test_capacity_eviction_scoped_per_project(tmp_path) -> None:
    """A busy project's capacity sweep never evicts a quiet project's memories sharing the SAME
    runtime.sqlite file: over-capacity eviction is scope-filtered, same as recall. (Two separate
    ``:memory:`` stores would be trivially isolated regardless of the scope column, so this test
    deliberately shares one file — the only way to exercise the filter at all.)"""
    db = str(tmp_path / "rt.db")
    store_busy = SQLiteStore(db, world_id="busy")
    store_quiet = SQLiteStore(db, world_id="quiet")
    embedder = HashingEmbedder()
    quiet_goal = "audit quest consistency for the caravan"
    _store_run(store_quiet, embedder, run_id="q1", goal=quiet_goal)
    for i in range(3):
        store_busy.save_agent_run(
            run_id=f"busy{i}",
            goal="busy goal",
            stop_reason="finished",
            step_count=1,
            cost_usd=0.0,
            latency_ms=0.0,
            agent_name="world-consistency",
            transcript_json="{}",
        )
        persist_run_embedding(
            store_busy,
            embedder,
            run_id=f"busy{i}",
            goal="busy goal",
            final_answer="done",
            capacity=2,
        )

    # The busy project's own capacity sweep (3 writes over capacity=2) evicted its own oldest entry
    # — never touching the quiet project's single memory in the same file.
    assert [h.run_id for h in recall_similar_runs(store_quiet, embedder, quiet_goal)] == ["q1"]
    busy_live = {
        r["run_id"] for r in store_busy.query_agent_run_embeddings(model_id=embedder.model_id)
    }
    assert len(busy_live) == 2  # capacity enforced within "busy"'s own scope
    store_busy.close()
    store_quiet.close()


# ====================================================== D3: audit-driven importance
# Importance stopped being a step-count proxy for how hard a run worked and became the size of the
# DETERMINISTIC AUDIT VERDICT it obtained — the project's "audit = verifiable reward" thesis applied
# to which episodes are worth remembering. The two runs below are exactly the two biases the module
# used to declare in its WEAKNESSES note, and they are the fixtures every test in this section uses.


def _short_but_pivotal_run() -> AgentResult:
    """Bias #1 under the old proxy: 2 steps, but it ran the deterministic checker and came back with
    twelve confirmed open errors. The step-count proxy scored this at 0.28 — near the bottom."""
    return AgentResult(
        goal=GOAL,
        final_answer="审计报出 12 个待修复错误，清单如下。",
        stop_reason="finished",
        step_count=2,
        steps=[
            AgentStep(action="build_context_pack", result={"chunks": []}),
            AgentStep(action="audit_project", result={"open_errors": 12}),
        ],
    )


def _long_but_fruitless_run() -> AgentResult:
    """Bias #2 under the old proxy: 9 steps of retrieval and failed lookups that never reached a
    checker and established nothing verifiable. The step-count proxy scored this at 0.78 — near the
    top, purely for having thrashed longer."""
    steps = [AgentStep(action="build_context_pack", result={"chunks": []}) for _ in range(6)]
    steps += [
        AgentStep(action="impact_of", is_error=True, observation="Error: NOT_FOUND")
        for _ in range(3)
    ]
    return AgentResult(
        goal=GOAL,
        final_answer="没能定位到问题。",
        stop_reason="finished",
        step_count=9,
        steps=steps,
    )


def test_run_audit_signal_counts_verified_outcomes_not_effort() -> None:
    """The signal itself: a checker verdict scores 1 (it was obtained at all) plus the size of what
    it carried; a trajectory that never reached a checker scores nothing, however long it was."""
    assert run_audit_signal(_short_but_pivotal_run()) == 1 + 12
    assert run_audit_signal(_long_but_fruitless_run()) == 0


def test_run_audit_signal_reads_repairs_and_ignores_non_verdicts() -> None:
    """The three payload shapes, and the three things that must NOT inflate the signal: alternative
    repair candidates for one issue (best, not sum), non-error severities, and a failed call."""
    result = AgentResult(
        goal=GOAL,
        final_answer="done",
        stop_reason="finished",
        step_count=4,
        steps=[
            AgentStep(
                action="propose_fix",
                result={
                    "candidates": [{"resolved_error_count": 2}, {"resolved_error_count": 3}],
                },
            ),
            AgentStep(
                action="quality_harness", result={"audit_totals": {"error": 4, "warning": 9}}
            ),
            # A checker call that FAILED establishes nothing, so it contributes nothing.
            AgentStep(action="audit_project", is_error=True, observation="Error: NOT_FOUND"),
            # A read tool returns content, not a verdict — not a checker at all.
            AgentStep(action="read_observation", result={"text": "..."}),
        ],
    )

    # propose_fix: 1 + best(3), NOT 1 + (2+3). quality_harness: 1 + errors(4), warnings excluded.
    assert run_audit_signal(result) == (1 + 3) + (1 + 4)


def test_importance_reverses_the_two_biases_the_step_count_proxy_had() -> None:
    """The headline of D3, stated as the inversion it is.

    Both directions are asserted from the same two fixtures: with a recorded audit signal the short
    verified run outranks the long fruitless one; with no recorded signal (a legacy row) the pre-D3
    proxy is still in force and orders them the OTHER way round — which is exactly the bias."""
    short = {"step_count": 2, "audit_signal": run_audit_signal(_short_but_pivotal_run())}
    fruitless = {"step_count": 9, "audit_signal": run_audit_signal(_long_but_fruitless_run())}

    assert _run_importance(short) > _run_importance(fruitless)

    legacy_short = {"step_count": short["step_count"], "audit_signal": None}
    legacy_fruitless = {"step_count": fruitless["step_count"], "audit_signal": None}
    assert _run_importance(legacy_short) < _run_importance(legacy_fruitless)


def test_persist_agent_run_records_the_audit_signal_on_the_row() -> None:
    """The write wiring: the signal is lifted onto the run row by the ordinary persist path, so
    recall never re-parses a transcript to rank an episode."""
    store = SQLiteStore(":memory:")
    persist_agent_run(store, _short_but_pivotal_run(), run_id="pivotal")
    persist_agent_run(store, _long_but_fruitless_run(), run_id="fruitless")

    signals = {
        str(row["id"]): row["audit_signal"]
        for row in store.conn.execute("SELECT id, audit_signal FROM agent_runs")
    }
    assert signals == {"pivotal": 13, "fruitless": 0}


def test_recall_ranks_the_short_verified_run_above_the_long_fruitless_one() -> None:
    """End to end through the production write + recall path: with relevance and recency tied, the
    run that verified something is the one recalled first — the opposite of the pre-D3 ordering."""
    store = SQLiteStore(":memory:")
    # q=[1,0,0]; both stored vectors are cosine .707 to it (relevance ties) and mutual .5 (no
    # dedup), and both embeddings are written "now" (recency ties) — so importance alone orders.
    embedder = _DictEmbedder({"Q": [1.0, 0.0, 0.0]})
    now_iso = datetime.now(UTC).isoformat()
    for run_id, result, vector in (
        ("pivotal", _short_but_pivotal_run(), [1.0, 1.0, 0.0]),
        ("fruitless", _long_but_fruitless_run(), [1.0, 0.0, 1.0]),
    ):
        persist_agent_run(store, result, run_id=run_id)
        store.save_agent_run_embedding(
            run_id=run_id,
            model_id="dict-3",
            goal=GOAL,
            summary="done",
            vector=_to_blob(vector),
            created_at=now_iso,
        )

    hits = recall_similar_runs(store, embedder, "Q")

    assert [h.run_id for h in hits] == ["pivotal", "fruitless"]
    # And the loser is the one with MORE steps — i.e. the ordering really did invert the proxy.
    rows = store.conn.execute("SELECT id, step_count FROM agent_runs")
    steps = {str(row["id"]): int(row["step_count"]) for row in rows}
    assert steps["pivotal"] < steps["fruitless"]


def _build_legacy_memory_db(path: str) -> None:
    """Pre-D1/D2 shape: agent_run_embeddings/reflections exist with the T7 `deleted_at` tombstone
    column but WITHOUT `last_accessed_at`/`scope`, each carrying one legacy row."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE agent_run_embeddings (
            run_id TEXT PRIMARY KEY, model_id TEXT NOT NULL, goal TEXT NOT NULL,
            summary TEXT NOT NULL, vector BLOB NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, deleted_at TEXT
        );
        CREATE TABLE reflections (
            id TEXT PRIMARY KEY, goal TEXT NOT NULL, reflection TEXT NOT NULL,
            source TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            vector BLOB, model_id TEXT, deleted_at TEXT
        );
        INSERT INTO agent_run_embeddings (run_id, model_id, goal, summary, vector, created_at)
            VALUES ('legacy_run', 'hashing-1024', 'legacy goal', 'legacy summary',
                    X'0000803f', '2026-01-01T00:00:00+00:00');
        INSERT INTO reflections (id, goal, reflection, source, created_at, vector, model_id)
            VALUES ('legacy_refl', 'legacy goal', 'legacy lesson', 'multi_agent_retry',
                    '2026-01-01T00:00:00+00:00', X'0000803f', 'hashing-1024');
        """
    )
    conn.commit()
    conn.close()


def test_legacy_memory_db_upgrades_in_place_with_default_scope(tmp_path) -> None:
    """A runtime DB predating D1/D2 upgrades in place: the new columns appear, the pre-existing rows
    survive untouched, and back-fill to the canonical 'default' scope / NULL `last_accessed_at` —
    i.e. a single-world project's pre-D1/D2 recall behaviour is unchanged."""
    db = str(tmp_path / "legacy_memory.db")
    _build_legacy_memory_db(db)

    store = SQLiteStore(db)  # opening runs initialize() -> migration
    try:
        run_cols = {
            row["name"] for row in store.conn.execute("PRAGMA table_info(agent_run_embeddings)")
        }
        refl_cols = {row["name"] for row in store.conn.execute("PRAGMA table_info(reflections)")}
        assert {"last_accessed_at", "scope"} <= run_cols
        assert {"last_accessed_at", "scope"} <= refl_cols

        run_row = store.conn.execute(
            "SELECT scope, last_accessed_at FROM agent_run_embeddings WHERE run_id = 'legacy_run'"
        ).fetchone()
        assert run_row["scope"] == "default"
        assert run_row["last_accessed_at"] is None

        refl_row = store.conn.execute(
            "SELECT scope, last_accessed_at FROM reflections WHERE id = 'legacy_refl'"
        ).fetchone()
        assert refl_row["scope"] == "default"
        assert refl_row["last_accessed_at"] is None

        idx = {
            row["name"]
            for row in store.conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        assert "idx_agent_run_embeddings_scope" in idx
        assert "idx_reflections_scope" in idx
    finally:
        store.close()
