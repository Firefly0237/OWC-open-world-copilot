"""Phase B implementation tests — B4-R1.

Covers all [硬] acceptance criteria for the 4 items:
- IN-B2 T2: SkillRegistry.manifest(allowed=...) + ReActAgent.allowed_skills (post-T2 host: the
  allowed set filters the BOUND TOOL SCHEMA — the manifest's function-calling equivalent)
- IN-B4 C1: AgentStep.observation_chars/is_truncated (the T3 text-path transcript trim retired
  with the text loop; the token budget is enforced by the compression middleware, pinned in
  test_agent_middleware.py / test_t4_context_compressor.py)
- IN-B1 M2: dimension-aware lessons (FalsePassItem, save/get, extract, primary_failing_dimension)
- IN-B3 M1: build_critic_lesson_block + critic lesson injection (all 6 critics, default off)
"""

from __future__ import annotations

import inspect

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from pydantic import Field as PydField

from owcopilot.agent.react import AgentStep, ReActAgent
from owcopilot.assist.calibration import (
    CalibrationReport,
    FalsePassItem,
    build_calibration_report,
    critic_from_trail,
    primary_dim_from_trail,
    primary_failing_dimension,
)
from owcopilot.assist.critic import (
    BarkCritic,
    CharacterCritic,
    CritiqueDimension,
    CritiqueResult,
    DialogueCritic,
    FlavorCritic,
    QuestCritic,
)
from owcopilot.assist.lessons import (
    build_critic_lesson_block,
    build_lesson_block,
    extract_lessons_from_report,
)
from owcopilot.assist.review_queue import ReviewItemType, ReviewQueue
from owcopilot.fakes import scripted_chat_model
from owcopilot.llm.cache import NoOpCache
from owcopilot.llm.gateway import LLMGateway
from owcopilot.llm.router import StaticRouter
from owcopilot.skills import (
    CostTier,
    SideEffect,
    Skill,
    SkillParameter,
    SkillRegistry,
)
from owcopilot.storage.sqlite import SQLiteStore
from owcopilot.worldgen.critic import WorldQuestCritic, run_quest_refine_loop

# ============================================================
# Helpers
# ============================================================


def _make_store() -> SQLiteStore:
    return SQLiteStore(":memory:")


def _echo_skill(name: str) -> Skill:
    return Skill(
        name=name,
        description=f"Echo skill {name}",
        cost_tier=CostTier.DETERMINISTIC,
        side_effect=SideEffect.READ_ONLY,
        handler=lambda **kw: {"name": name},
        parameters=(SkillParameter("query", "string", "A query."),),
    )


def _make_registry(*names: str) -> SkillRegistry:
    reg = SkillRegistry()
    for n in names:
        reg.register(_echo_skill(n))
    return reg


class _ToolRecordingModel(GenericFakeChatModel):
    """Scripted chat fake recording the bound tool names and every call's message list."""

    bound: list[list[str]] = PydField(default_factory=list)
    seen: list[list[object]] = PydField(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        self.bound.append([t.name for t in tools])
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(list(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


# ============================================================
# IN-B2 T2 — SkillRegistry.manifest(allowed=...) + ReActAgent.allowed_skills
# ============================================================


class TestT2ManifestFilter:
    """H-T2-1 through H-T2-7"""

    def test_H_T2_1_manifest_no_args_equals_manifest_allowed_none(self):
        """H-T2-1: manifest() == manifest(allowed=None) byte-for-byte."""
        reg = _make_registry("skill_a", "skill_b", "skill_c")
        assert reg.manifest() == reg.manifest(allowed=None)

    def test_H_T2_1_manifest_none_equals_all_skills_join(self):
        """H-T2-1: manifest(allowed=None) == join of all manifest_line()."""
        reg = _make_registry("skill_a", "skill_b")
        expected = "\n".join(s.manifest_line() for s in reg)
        assert reg.manifest() == expected
        assert reg.manifest(allowed=None) == expected

    def test_H_T2_2_allowed_set_filters_to_named_skill(self):
        """H-T2-2: allowed={"skill_a"} returns only skill_a manifest line."""
        reg = _make_registry("skill_a", "skill_b", "skill_c")
        m = reg.manifest(allowed={"skill_a"})
        assert "skill_a" in m
        for name in ["skill_b", "skill_c"]:
            assert name not in m

    def test_H_T2_3_unknown_name_in_allowed_silently_ignored(self):
        """H-T2-3: unknown names in allowed set are silently ignored."""
        reg = _make_registry("skill_a", "skill_b")
        m = reg.manifest(allowed={"skill_a", "nonexistent_skill_xyz"})
        assert "nonexistent_skill_xyz" not in m
        assert "skill_a" in m
        assert "skill_b" not in m

    def test_H_T2_4_empty_set_returns_empty_string(self):
        """H-T2-4: allowed=set() returns ''."""
        reg = _make_registry("skill_a", "skill_b")
        assert reg.manifest(allowed=set()) == ""

    def test_H_T2_5_react_agent_none_binds_same_tools_as_no_param(self):
        """H-T2-5: allowed_skills=None binds the same tool set as not passing allowed_skills."""
        reg = _make_registry("skill_a", "skill_b")
        model_old = _ToolRecordingModel(messages=iter([AIMessage(content="ok")]))
        model_new = _ToolRecordingModel(messages=iter([AIMessage(content="ok")]))

        ReActAgent(model=model_old, registry=reg).run("test")
        ReActAgent(model=model_new, registry=reg, allowed_skills=None).run("test")

        assert model_old.bound[-1] == model_new.bound[-1] == ["skill_a", "skill_b"]

    def test_H_T2_6_allowed_skills_set_filters_bound_tools(self):
        """H-T2-6: allowed_skills={"skill_a"} exposes only skill_a to the model."""
        reg = _make_registry("skill_a", "skill_b", "skill_c")
        model = _ToolRecordingModel(messages=iter([AIMessage(content="ok")]))
        ReActAgent(model=model, registry=reg, allowed_skills={"skill_a"}).run("test")
        assert model.bound[-1] == ["skill_a"]

    def test_H_T2_7_model_calls_blocked_skill_gets_error_not_crash(self):
        """H-T2-7: model calling a tool outside its bound set gets an error observation (or the
        execution-layer dispatch, for a registered skill), never a crash."""
        reg = _make_registry("skill_a", "skill_b")
        # skill_b IS registered, so the execution-layer registry dispatch still runs it (the
        # historical contract: the schema filter hides, the registry gate decides) — no crash.
        agent = ReActAgent(
            model=scripted_chat_model([("skill_b", {}), "recovered"]),
            registry=reg,
            allowed_skills={"skill_a"},
            max_steps=3,
        )
        result = agent.run("test")
        assert result.stop_reason == "finished"

        # A truly unknown skill becomes a recoverable error observation.
        agent2 = ReActAgent(
            model=scripted_chat_model([("nonexistent_tool", {}), "recovered"]),
            registry=reg,
            allowed_skills={"skill_a"},
            max_steps=3,
        )
        result2 = agent2.run("test")
        assert result2.stop_reason == "finished"
        error_steps = [s for s in result2.steps if s.is_error]
        assert any("nonexistent_tool" in s.observation for s in error_steps)


# ============================================================
# IN-B4 C1 — AgentStep observation fields (the T3 text-path trim helpers retired with the
# text loop; token-budget behaviour is pinned by the compression-middleware tests)
# ============================================================


class TestC1T3TranscriptBudget:
    """H-C1-1 through H-C1-12 (budget-helper items superseded by the compression middleware)"""

    def test_H_C1_1_agent_step_observation_chars_default_zero(self):
        """H-C1-1: AgentStep().observation_chars == 0."""
        step = AgentStep()
        assert step.observation_chars == 0
        assert isinstance(step.observation_chars, int)

    def test_H_C1_2_agent_step_is_truncated_default_false(self):
        """H-C1-2: AgentStep().is_truncated == False."""
        step = AgentStep()
        assert step.is_truncated is False

    def test_H_C1_3_observation_chars_equals_len_observation_after_truncation(self):
        """H-C1-3: observation_chars == len(step.observation) after truncation."""
        reg = _make_registry("skill_a")
        agent = ReActAgent(
            model=scripted_chat_model([("skill_a", {}), "ok"]), registry=reg, max_steps=3
        )
        result = agent.run("test")
        assert result.steps
        for step in result.steps:
            if step.action:  # only action steps have obs
                assert step.observation_chars == len(step.observation)

    def test_H_C1_4_is_truncated_true_when_obs_truncated(self):
        """H-C1-4: is_truncated=True when observation exceeds observation_char_limit."""
        # Make a skill that returns a large result
        large_result = {"data": "x" * 5000}
        long_skill = Skill(
            name="long_skill",
            description="Returns large data.",
            cost_tier=CostTier.DETERMINISTIC,
            side_effect=SideEffect.READ_ONLY,
            handler=lambda **kw: large_result,
            parameters=(),
        )
        reg = SkillRegistry()
        reg.register(long_skill)

        agent = ReActAgent(
            model=scripted_chat_model([("long_skill", {}), "ok"]),
            registry=reg,
            observation_char_limit=100,  # small limit -> truncation
            max_steps=3,
        )
        result = agent.run("test")
        action_steps = [s for s in result.steps if s.action == "long_skill"]
        assert len(action_steps) == 1
        assert action_steps[0].is_truncated is True

    def test_H_C1_4_is_truncated_false_when_obs_fits(self):
        """H-C1-4: is_truncated=False when observation fits within limit."""
        small_skill = Skill(
            name="small_skill",
            description="Small result.",
            cost_tier=CostTier.DETERMINISTIC,
            side_effect=SideEffect.READ_ONLY,
            handler=lambda **kw: {"ok": True},
            parameters=(),
        )
        reg = SkillRegistry()
        reg.register(small_skill)

        agent = ReActAgent(
            model=scripted_chat_model([("small_skill", {}), "ok"]),
            registry=reg,
            observation_char_limit=10000,  # large limit -> no truncation
            max_steps=3,
        )
        result = agent.run("test")
        action_steps = [s for s in result.steps if s.action == "small_skill"]
        assert len(action_steps) == 1
        assert action_steps[0].is_truncated is False

    # H-C1-5..10 and the _trim_transcript unit tests covered the text path's _user_prompt /
    # _trim_transcript safety-net helpers, deleted with the text loop. The transcript token
    # budget is now enforced by ContextCompressionMiddleware, pinned in
    # test_agent_middleware.py and test_t4_context_compressor.py.

    def test_H_C1_11_no_regression_default_budget(self):
        """H-C1-11: ReActAgent with default budget completes normally (no regression)."""
        reg = _make_registry("skill_a")
        agent = ReActAgent(model=scripted_chat_model(["ok"]), registry=reg)
        result = agent.run("test goal")
        assert result.stop_reason == "finished"
        assert result.final_answer == "ok"

    def test_H_C1_12_budget_none_behavior_consistent(self):
        """H-C1-12: transcript_char_budget=None -> same behavior as no budget param."""
        reg = _make_registry("skill_a")
        model1 = _ToolRecordingModel(messages=iter([AIMessage(content="ok")]))
        model2 = _ToolRecordingModel(messages=iter([AIMessage(content="ok")]))

        agent1 = ReActAgent(model=model1, registry=reg, transcript_char_budget=None)
        agent2 = ReActAgent(model=model2, registry=reg)

        result1 = agent1.run("goal")
        result2 = agent2.run("goal")

        assert result1.final_answer == result2.final_answer
        # With no steps, the first model call sees identical context either way.
        assert [str(m.text) for m in model1.seen[0]] == [str(m.text) for m in model2.seen[0]]


# ============================================================
# IN-B1 M2 — Lesson dimension細化
# ============================================================


class TestM2LessonDimension:
    """H-M2-1 through H-M2-9"""

    def test_H_M2_1_false_pass_item_has_dimension_field_default_general(self):
        """H-M2-1: FalsePassItem has dimension field, default 'general'."""
        assert hasattr(FalsePassItem, "model_fields")
        assert "dimension" in FalsePassItem.model_fields
        assert FalsePassItem.model_fields["dimension"].default == "general"

    def test_H_M2_1_false_pass_item_backward_compat(self):
        """H-M2-1: Old-style FalsePassItem creation (no dimension) still works."""
        item = FalsePassItem(item_id="x", item_type="quest_draft", object_ref="q:1")
        assert item.dimension == "general"

    def test_H_M2_2_save_lesson_no_dimension_defaults_general(self):
        """H-M2-2: save_lesson without dimension -> dimension='general' in DB."""
        store = _make_store()
        store.save_lesson("quest", "test text")
        row = store.conn.execute("SELECT dimension FROM lessons WHERE item_type='quest'").fetchone()
        assert row is not None
        assert row["dimension"] == "general"

    def test_H_M2_2_save_lesson_with_dimension(self):
        """H-M2-2: save_lesson(dimension='grounding') stores grounding."""
        store = _make_store()
        store.save_lesson("quest", "text2", dimension="grounding")
        row = store.conn.execute(
            "SELECT dimension FROM lessons WHERE item_type='quest' AND dimension='grounding'"
        ).fetchone()
        assert row is not None

    def test_H_M2_3_same_type_same_dim_accumulates_count(self):
        """H-M2-3: same (item_type, dimension) → false_pass_count accumulates, single row."""
        store = _make_store()
        store.save_lesson("quest", "text", dimension="intent")
        store.save_lesson("quest", "text2", dimension="intent")
        rows = store.conn.execute(
            "SELECT * FROM lessons WHERE item_type='quest' AND dimension='intent'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["false_pass_count"] == 2

    def test_H_M2_4_different_dims_coexist(self):
        """H-M2-4: same type, different dimensions → two separate rows."""
        store = _make_store()
        store.save_lesson("quest", "t1", dimension="intent")
        store.save_lesson("quest", "t2", dimension="grounding")
        count = store.conn.execute(
            "SELECT COUNT(*) FROM lessons WHERE item_type='quest'"
        ).fetchone()[0]
        assert count == 2

    def test_H_M2_5_extract_below_threshold_not_written(self):
        """H-M2-5: (type, dim) with < min_false_pass → not written; only sufficient ones."""
        store = _make_store()
        # 2 intent items (below threshold=3), 5 grounding items (above)
        intent_items = [
            FalsePassItem(item_id=f"i{i}", item_type="quest", object_ref="r", dimension="intent")
            for i in range(2)
        ]
        grounding_items = [
            FalsePassItem(item_id=f"j{i}", item_type="quest", object_ref="r", dimension="grounding")
            for i in range(5)
        ]
        items = intent_items + grounding_items
        report = CalibrationReport(false_pass_items=items)
        written = extract_lessons_from_report(report, store, min_false_pass=3)
        assert written == 1  # only grounding (5 >= 3)
        rows = store.conn.execute("SELECT dimension FROM lessons").fetchall()
        assert all(r["dimension"] == "grounding" for r in rows)

    def test_H_M2_6_get_lessons_dimension_filter(self):
        """H-M2-6: get_lessons_for_type(dimension='grounding') only returns grounding lessons."""
        store = _make_store()
        store.save_lesson("quest", "lesson grounding", dimension="grounding")
        store.save_lesson("quest", "lesson intent", dimension="intent")
        lessons = store.get_lessons_for_type("quest", dimension="grounding")
        assert all(r["dimension"] == "grounding" for r in lessons)
        assert len(lessons) == 1

    def test_H_M2_7_get_lessons_no_dimension_returns_all(self):
        """H-M2-7: get_lessons_for_type without dimension → returns all dimensions."""
        store = _make_store()
        store.save_lesson("quest", "lesson grounding", dimension="grounding")
        store.save_lesson("quest", "lesson intent", dimension="intent")
        lessons = store.get_lessons_for_type("quest")  # no dimension filter
        assert len(lessons) >= 2

    def test_H_M2_8_primary_failing_dimension_parse_fail_returns_general(self):
        """H-M2-8: parse_ok=False → 'general'."""
        result = CritiqueResult(verdict="revise", score=0, parse_ok=False)
        assert primary_failing_dimension(result) == "general"

    def test_H_M2_8_primary_failing_dimension_blocker_wins(self):
        """H-M2-8: blocker dimension wins over minor."""
        result = CritiqueResult(
            verdict="revise",
            score=0.3,
            parse_ok=True,
            dimensions=[
                CritiqueDimension(dimension="grounding", severity="blocker", issue="x"),
                CritiqueDimension(dimension="craft", severity="minor", issue="y"),
            ],
        )
        assert primary_failing_dimension(result) == "grounding"

    def test_H_M2_8_primary_failing_dimension_minor_only(self):
        """H-M2-8: only minor → returns first minor dimension."""
        result = CritiqueResult(
            verdict="revise",
            score=0.6,
            parse_ok=True,
            dimensions=[CritiqueDimension(dimension="craft", severity="minor", issue="z")],
        )
        assert primary_failing_dimension(result) == "craft"

    def test_H_M2_8_primary_failing_dimension_no_dims_returns_general(self):
        """H-M2-8: parse_ok=True but no dimensions → 'general'."""
        result = CritiqueResult(verdict="pass", score=0.9, parse_ok=True, dimensions=[])
        assert primary_failing_dimension(result) == "general"

    def test_H_M2_8_primary_failing_dimension_all_ok_returns_general(self):
        """H-M2-8: all dimensions are 'ok' → 'general'."""
        result = CritiqueResult(
            verdict="pass",
            score=0.9,
            parse_ok=True,
            dimensions=[
                CritiqueDimension(dimension="intent", severity="ok", issue=""),
                CritiqueDimension(dimension="craft", severity="ok", issue=""),
            ],
        )
        assert primary_failing_dimension(result) == "general"

    def test_H_M2_9_save_lesson_backward_compat_no_type_error(self):
        """H-M2-9: old-style call save_lesson(type, text) without dimension → no TypeError."""
        store = _make_store()
        store.save_lesson("quest", "old style call")  # must not raise

    def test_extract_lessons_general_dimension_template(self):
        """extract_lessons uses general template when dimension='general'."""
        store = _make_store()
        items = [
            FalsePassItem(
                item_id=f"i{i}", item_type="quest_draft", object_ref="r", dimension="general"
            )
            for i in range(3)
        ]
        report = CalibrationReport(false_pass_items=items)
        extract_lessons_from_report(report, store, min_false_pass=3)
        lessons = store.get_lessons_for_type("quest_draft", dimension="general")
        assert len(lessons) == 1
        text = lessons[0]["lesson_text"]
        assert "quest_draft" in text

    def test_extract_lessons_specific_dimension_template(self):
        """extract_lessons uses dimension-specific template for non-general dims."""
        store = _make_store()
        items = [
            FalsePassItem(
                item_id=f"i{i}", item_type="quest_draft", object_ref="r", dimension="intent"
            )
            for i in range(3)
        ]
        report = CalibrationReport(false_pass_items=items)
        extract_lessons_from_report(report, store, min_false_pass=3)
        lessons = store.get_lessons_for_type("quest_draft", dimension="intent")
        assert len(lessons) == 1
        text = lessons[0]["lesson_text"]
        assert "intent" in text

    def test_primary_dim_from_trail_empty_returns_none(self):
        """primary_dim_from_trail([]) returns None."""
        assert primary_dim_from_trail([]) is None

    def test_primary_dim_from_trail_parse_fail_returns_none(self):
        """primary_dim_from_trail with unparseable last step returns None."""
        trail = [
            {"verdict": "revise", "score": 0.0, "auto_review_ok": False, "primary_dim": "intent"}
        ]
        assert primary_dim_from_trail(trail) is None

    def test_primary_dim_from_trail_ok_returns_dim(self):
        """primary_dim_from_trail with parseable step returns primary_dim."""
        trail = [
            {"verdict": "pass", "score": 0.9, "auto_review_ok": True, "primary_dim": "grounding"}
        ]
        assert primary_dim_from_trail(trail) == "grounding"


# ============================================================
# IN-B1 M2 — worldgen genesis path end-to-end dimension chain
# (FAIL-1 fix: WorldRefineRound.primary_dim → trail dump → review queue →
#  calibration false_pass.dimension → extract_lessons non-general lesson)
# ============================================================

# A critique JSON whose only failing dimension is a 'grounding' blocker. The real
# tolerant parser (assist.critic.parse_critique) consumes this — no fake parsing.
_WORLD_GROUNDING_BLOCKER = (
    '{"verdict": "revise", "score": 0.3, "summary": "giver invented", '
    '"dimensions": [{"dimension": "grounding", "severity": "blocker", '
    '"issue": "giver_npc references an NPC the world does not contain", '
    '"fix": "use an existing cast id"}]}'
)


class _WorldCritiqueProvider:
    """Returns a fixed critique reply for every world_seed critique call."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls = 0

    def complete(self, *, system: str, user: str, model: str):
        self.calls += 1
        return self.reply, 10, 5


def _world_gateway(provider) -> LLMGateway:
    return LLMGateway(
        providers={"cheap": provider},
        router=StaticRouter(mapping={"world_seed": "cheap"}),
        cache=NoOpCache(),
    )


class TestM2WorldgenGenesisChain:
    """FAIL-1: the worldgen genesis path must carry a real (non-'general') dimension
    through the entire M2 chain, exactly like the four assist paths already do."""

    def _run_loop_once(self) -> list:
        """Drive the real run_quest_refine_loop for ONE round with a grounding-blocker
        critique. regenerate is never expected to be called past the cap; if it is, it
        returns the same quests so the loop terminates."""
        critic = WorldQuestCritic(
            gateway=_world_gateway(_WorldCritiqueProvider(_WORLD_GROUNDING_BLOCKER))
        )
        quests = [
            {
                "id": "q1",
                "title": "Q1",
                "objective": "do a thing",
                "stages": [{"id": "s1"}, {"id": "s2"}],
                "giver_npc": "npc:hero",
                "location": "region:town",
            }
        ]

        def regenerate(prior, fixes):
            return prior, [], []

        outcome = run_quest_refine_loop(
            critic=critic,
            max_rounds=1,
            quests=quests,
            relations=[],
            reference_rows=[],
            npc_refs={"npc:hero"},
            place_refs={"region:town"},
            context_lines=["npc:hero", "region:town"],
            brief="a grounded world",
            regenerate=regenerate,
            emit=lambda _stage: None,
        )
        return outcome.trail

    def test_world_refine_round_has_primary_dim_field(self):
        """WorldRefineRound exposes primary_dim (default 'general')."""
        from owcopilot.worldgen.models import WorldRefineRound

        assert "primary_dim" in WorldRefineRound.model_fields
        assert WorldRefineRound.model_fields["primary_dim"].default == "general"

    def test_loop_writes_real_dimension_not_general(self):
        """The refine loop stamps primary_dim='grounding' (the blocker), not 'general'."""
        trail = self._run_loop_once()
        assert trail, "expected at least one refine round"
        assert trail[-1].primary_dim == "grounding"

    def test_trail_dump_round_trips_through_primary_dim_from_trail(self):
        """model_dump(json) → primary_dim_from_trail recovers 'grounding'."""
        trail = self._run_loop_once()
        dump = [r.model_dump(mode="json") for r in trail]
        assert primary_dim_from_trail(dump) == "grounding"

    def test_add_world_seed_stamps_critic_primary_dim(self):
        """add_world_seed persists critic_primary_dim onto the ReviewItem + DB row."""
        trail = self._run_loop_once()
        dump = [r.model_dump(mode="json") for r in trail]
        verdict, score = critic_from_trail(dump)
        item = ReviewQueue(_make_store()).add_world_seed(
            {"id": "w1", "bundle": {}},
            critic_verdict=verdict,
            critic_score=score,
            critic_primary_dim=primary_dim_from_trail(dump),
        )
        assert item.item_type is ReviewItemType.WORLD_SEED
        assert item.critic_primary_dim == "grounding"

    def test_full_chain_genesis_false_pass_produces_grounding_lesson(self):
        """End-to-end: 3 worldgen genesis false-passes (critic said pass, human rejected),
        each carrying a real critic_primary_dim, must extract a 'grounding' lesson — NOT
        'general'. This proves the worldgen path drives M2 dimension细化 for real."""
        store = _make_store()
        # The calibration false-pass requires critic_verdict='pass' but human 'rejected'.
        # The dimension is whatever the critic flagged in its (passing-verdict) review —
        # here we stamp 'grounding' as the recorded primary dim, exactly as the wired
        # genesis path would when the critic passed an item the human later bounced.
        resolved = []
        for i in range(3):
            item = ReviewQueue(store).add_world_seed(
                {"id": f"w{i}", "bundle": {}},
                critic_verdict="pass",
                critic_score=0.85,
                critic_primary_dim="grounding",
            )
            item.status = "rejected"
            resolved.append(item)

        report = build_calibration_report(resolved)
        # All three false-passes recorded with the real dimension, not 'general'.
        assert len(report.false_pass_items) == 3
        assert all(fp.dimension == "grounding" for fp in report.false_pass_items)
        assert all(fp.item_type == "world_seed" for fp in report.false_pass_items)

        written = extract_lessons_from_report(report, store, min_false_pass=3)
        assert written == 1
        lessons = store.get_lessons_for_type("world_seed", dimension="grounding")
        assert len(lessons) == 1
        assert lessons[0]["dimension"] == "grounding"
        # And NO 'general' lesson leaked from this path.
        general = store.get_lessons_for_type("world_seed", dimension="general")
        assert general == []


# ============================================================
# IN-B3 M1 — Critic lesson injection (build_critic_lesson_block + all 6 critics)
# ============================================================


class TestM1CriticLessonInjection:
    """H-M1-1 through H-M1-8"""

    def test_H_M1_1_all_5_assist_critics_accept_lessons_params(self):
        """H-M1-1: All 5 assist critics accept lessons and inject_lessons params."""
        for cls in [QuestCritic, CharacterCritic, DialogueCritic, BarkCritic, FlavorCritic]:
            sig = inspect.signature(cls.critique)
            assert "lessons" in sig.parameters, f"{cls.__name__} missing 'lessons'"
            assert "inject_lessons" in sig.parameters, f"{cls.__name__} missing 'inject_lessons'"
            assert sig.parameters["inject_lessons"].default is False, (
                f"{cls.__name__} inject_lessons default must be False"
            )

    def test_H_M1_1_world_quest_critic_accepts_lessons_params(self):
        """H-M1-1: WorldQuestCritic accepts lessons and inject_lessons params."""
        sig = inspect.signature(WorldQuestCritic.critique)
        assert "lessons" in sig.parameters
        assert "inject_lessons" in sig.parameters
        assert sig.parameters["inject_lessons"].default is False

    def test_H_M1_5_critic_block_header_different_from_generation_side(self):
        """H-M1-5: [critic-lesson-memory] vs [lesson-memory] — must not be mixed."""
        gen_block = build_lesson_block([{"lesson_text": "x"}], inject_lessons=True)
        critic_block = build_critic_lesson_block([{"lesson_text": "x"}], inject_lessons=True)
        assert "[lesson-memory]" in gen_block
        assert "[critic-lesson-memory]" in critic_block
        assert "[critic-lesson-memory]" not in gen_block
        assert "[lesson-memory]" not in critic_block

    def test_H_M1_6_critic_block_contains_blocker_keyword(self):
        """H-M1-6: critic lesson block contains 'blocker' keyword."""
        block = build_critic_lesson_block(
            [{"lesson_text": "x", "dimension": "intent"}], inject_lessons=True
        )
        assert "blocker" in block.lower()

    def test_H_M1_7_empty_lessons_returns_empty_string(self):
        """H-M1-7: build_critic_lesson_block([], inject_lessons=True) == ''."""
        assert build_critic_lesson_block([], inject_lessons=True) == ""

    def test_H_M1_7_inject_lessons_false_returns_empty(self):
        """H-M1-7: inject_lessons=False always returns ''."""
        lessons = [{"lesson_text": "x", "dimension": "intent"}]
        assert build_critic_lesson_block(lessons, inject_lessons=False) == ""

    def test_H_M1_8_at_most_3_lessons_injected(self):
        """H-M1-8: more than 3 lessons → at most 3 appear in block."""
        lessons = [{"lesson_text": f"t{i}", "dimension": "craft"} for i in range(10)]
        block = build_critic_lesson_block(lessons, inject_lessons=True)
        count = sum(1 for i in range(10) if f"t{i}" in block)
        assert count <= 3

    def test_H_M1_3_inject_true_nonempty_lessons_includes_header(self):
        """H-M1-3: inject_lessons=True + non-empty lessons → [critic-lesson-memory] in output."""
        lessons = [{"lesson_text": "历史有3次被拒", "dimension": "intent"}]
        block = build_critic_lesson_block(lessons, inject_lessons=True)
        assert "[critic-lesson-memory]" in block

    def test_H_M1_4_inject_true_includes_lesson_text(self):
        """H-M1-4: inject_lessons=True → lesson text appears in block."""
        lessons = [{"lesson_text": "历史有3次被拒", "dimension": "grounding"}]
        block = build_critic_lesson_block(lessons, inject_lessons=True)
        assert "历史有3次被拒" in block

    def test_M1_dimension_hint_shown_for_nongeral_dimensions(self):
        """Non-general dimension → dimension hint appears in lesson block."""
        lessons = [{"lesson_text": "bad at intent", "dimension": "intent"}]
        block = build_critic_lesson_block(lessons, inject_lessons=True)
        assert "intent" in block

    def test_M1_general_dimension_no_dim_hint_needed(self):
        """General dimension → block still works correctly."""
        lessons = [{"lesson_text": "generic lesson", "dimension": "general"}]
        block = build_critic_lesson_block(lessons, inject_lessons=True)
        assert "generic lesson" in block
        assert "[critic-lesson-memory]" in block

    def test_H_M1_2_inject_lessons_false_default_no_prompt_change(self):
        """H-M1-2: inject_lessons=False (default) leaves prompt with no lesson content."""
        lessons = [{"lesson_text": "some lesson", "dimension": "intent"}]
        # The default call (no lessons, no inject_lessons) must produce same output as
        # explicit inject_lessons=False
        block_default = build_critic_lesson_block([])
        block_explicit_off = build_critic_lesson_block(lessons, inject_lessons=False)
        assert block_default == ""
        assert block_explicit_off == ""

    def test_M1_block_ends_with_blocker_instruction(self):
        """The critic lesson block ends with the blocker upgrade instruction."""
        lessons = [{"lesson_text": "x", "dimension": "craft"}]
        block = build_critic_lesson_block(lessons, inject_lessons=True)
        lines = block.strip().splitlines()
        last_line = lines[-1]
        assert "blocker" in last_line.lower()


# ============================================================
# Cross-item backward-compat sanity checks
# ============================================================


class TestBackwardCompatibility:
    """Verify that all default paths are unchanged from pre-Phase-B behavior."""

    def test_skill_registry_manifest_no_args_unchanged(self):
        """manifest() with no args still works (H-T2-1)."""
        reg = _make_registry("a", "b")
        m = reg.manifest()
        assert "a" in m and "b" in m

    def test_agent_step_old_fields_still_present(self):
        """All original AgentStep fields still have default values."""
        step = AgentStep()
        assert step.thought == ""
        assert step.action == ""
        assert step.observation == ""
        assert step.is_error is False
        assert step.latency_ms == 0.0
        assert step.cost_usd == 0.0

    def test_false_pass_item_no_dimension_backward_compat(self):
        """FalsePassItem created without dimension defaults to 'general'."""
        item = FalsePassItem(
            item_id="abc",
            item_type="quest_draft",
            object_ref="quest:q1",
            critic_score=0.8,
        )
        assert item.dimension == "general"

    def test_save_lesson_old_style_no_dimension(self):
        """save_lesson(type, text) without dimension doesn't raise TypeError."""
        store = _make_store()
        store.save_lesson("quest_draft", "a lesson")
        lessons = store.get_lessons_for_type("quest_draft")
        assert len(lessons) == 1

    def test_get_lessons_for_type_no_dimension_returns_all(self):
        """get_lessons_for_type without dimension param returns all (backward compat)."""
        store = _make_store()
        store.save_lesson("t", "l1", dimension="intent")
        store.save_lesson("t", "l2", dimension="craft")
        lessons = store.get_lessons_for_type("t")
        assert len(lessons) == 2

    def test_build_lesson_block_unchanged(self):
        """build_lesson_block still works the same (generation side unchanged)."""
        lessons = [{"lesson_text": "lesson A"}]
        block = build_lesson_block(lessons, inject_lessons=True)
        assert "[lesson-memory]" in block
        assert "lesson A" in block
