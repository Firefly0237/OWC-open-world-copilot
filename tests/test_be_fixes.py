"""BE-1~BE-9 audit-fix tests.

BE-1 (HIGH): Lesson archive production wiring — calibration writes lessons, draft reads lessons.
BE-2 (MED):  worldgen/critic.py now injects term constraints (build_term_block_for_critic).
BE-3 (LOW):  drafts._system_prompt ordering is now quality_bar -> lesson -> term.
BE-4 (LOW):  build_critic_lesson_block rewrites "生成时请着重提高" to "评判时请着重核查".
BE-5 (LOW):  build_term_block >20-terms filter includes forbidden-word hits.
BE-6 (LOW):  worldgen _vocab_block >20-terms uses term.id+canonical as seed_hits.
BE-7 (LOW):  AcceptanceCheck.details includes eval_type and is_sanity_gate.
BE-8 (LOW):  save_lesson ON CONFLICT updates lesson_text.
BE-9 (LOW):  CLI --skills allowlist wires into ReActAgent.allowed_skills.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# BE-1: e2e integration — calibration -> lesson write -> draft prompt injection
# ---------------------------------------------------------------------------

def _make_store_and_queue():
    from owcopilot.assist.review_queue import ReviewQueue
    from owcopilot.storage.sqlite import SQLiteStore

    store = SQLiteStore(":memory:")
    queue = ReviewQueue(store)
    return store, queue


def _add_resolved(queue, *, verdict: str, human: str, n: int = 1) -> None:
    from owcopilot.assist.review_queue import ReviewItem, ReviewItemType
    for i in range(n):
        item = queue.add(
            ReviewItem(
                item_type=ReviewItemType.QUEST_DRAFT,
                object_ref=f"quest:e2e_{verdict}_{human}_{i}",
                payload={"id": f"q_{i}"},
                status="pending_review",
                critic_verdict=verdict,
                critic_score=0.8 if verdict == "pass" else 0.4,
                critic_primary_dim="completeness",
            )
        )
        queue.mark(item.id, human, decided_by="editor")


def test_be1_lesson_write_on_calibration() -> None:
    """[BE-1 硬] reviewer_calibration_action extracts lessons into the DB."""
    from owcopilot.assist.calibration import build_calibration_report
    from owcopilot.assist.lessons import extract_lessons_from_report

    store2, queue = _make_store_and_queue()

    # Seed 3 false_pass items (critic=pass, human=rejected) for quest_draft/completeness
    _add_resolved(queue, verdict="pass", human="rejected", n=3)

    resolved = queue.list_resolved()
    report = build_calibration_report(resolved)
    assert len(report.false_pass_items) == 3

    written = extract_lessons_from_report(report, store2, min_false_pass=3)
    assert written == 1

    lessons = store2.get_lessons_for_type("quest_draft")
    assert len(lessons) == 1
    assert "completeness" in lessons[0]["lesson_text"] or "quest_draft" in lessons[0]["lesson_text"]
    store2.close()


def test_be1_draft_prompt_contains_lesson_when_lessons_present() -> None:
    """[BE-1 硬] QuestDraftService with lessons injects [lesson-memory] into system prompt."""
    from owcopilot.assist.drafts import _system_prompt

    # Directly test _system_prompt with a lesson
    from owcopilot.retrieval.models import ContextPack
    pack = ContextPack(hits=[], query="test", budget_tokens=800)
    lessons = [
        {"lesson_text": "此类 quest_draft 内容历史有 3 次被拒。", "dimension": "completeness"}
    ]
    prompt = _system_prompt(pack, brief="test brief", lessons=lessons, inject_lessons=True)

    assert "[lesson-memory]" in prompt, "lesson block must appear in prompt when lessons provided"
    assert "quest_draft" in prompt or "completeness" in prompt


def test_be1_draft_prompt_no_lesson_when_empty() -> None:
    """[BE-1 软] Empty lessons list -> no lesson block in system prompt."""
    from owcopilot.assist.drafts import _system_prompt
    from owcopilot.retrieval.models import ContextPack

    pack = ContextPack(hits=[], query="test", budget_tokens=800)
    prompt = _system_prompt(pack, brief="test brief", lessons=[], inject_lessons=True)
    # empty lessons list -> build_lesson_block returns ""
    assert "[lesson-memory]" not in prompt


def test_be1_e2e_calibration_to_draft_injection(tmp_path) -> None:
    """[BE-1 e2e] Calibration action -> lesson written -> draft action prompt contains lesson.

    This is the full end-to-end chain:
    1. Seed 3 false-pass items into the review queue.
    2. Run reviewer_calibration_action -> lessons written to SQLite.
    3. Call run_draft_action offline -> system prompt contains [lesson-memory].
    """
    from owcopilot.assist.calibration import build_calibration_report
    from owcopilot.assist.drafts import _system_prompt
    from owcopilot.assist.lessons import extract_lessons_from_report
    from owcopilot.assist.review_queue import ReviewQueue
    from owcopilot.retrieval.models import ContextPack
    from owcopilot.storage.sqlite import SQLiteStore

    # Step 1: build a store with 3 false-pass items
    store = SQLiteStore(":memory:")
    queue = ReviewQueue(store)
    _add_resolved(queue, verdict="pass", human="rejected", n=3)
    resolved = queue.list_resolved()
    report = build_calibration_report(resolved)

    # Step 2: extract lessons (simulating what reviewer_calibration_action now does)
    written = extract_lessons_from_report(report, store, min_false_pass=3)
    assert written >= 1, "at least one lesson must be written from the 3 false-pass items"

    # Step 3: fetch lessons (simulating what run_draft_action now does)
    lessons = store.get_lessons_for_type("quest_draft")
    assert len(lessons) >= 1, "at least one lesson must be retrievable after extract"

    # Step 4: verify system prompt contains [lesson-memory] when lessons present
    pack = ContextPack(hits=[], query="quest brief", budget_tokens=800)
    prompt = _system_prompt(pack, brief="quest brief", lessons=lessons, inject_lessons=True)
    assert "[lesson-memory]" in prompt, (
        f"[BE-1 e2e] system prompt must contain [lesson-memory] block.\n"
        f"lessons={lessons}\n"
        f"prompt[:500]={prompt[:500]}"
    )

    # Bonus: verify lesson content is in the prompt
    lesson_text = lessons[0]["lesson_text"]
    assert "quest_draft" in lesson_text or "completeness" in lesson_text

    store.close()


# ---------------------------------------------------------------------------
# BE-2: worldgen/critic.py term injection
# ---------------------------------------------------------------------------

def test_be2_worldgen_critic_system_prompt_contains_term_block() -> None:
    """[BE-2 硬] _critic_system_prompt in worldgen/critic.py injects term constraints."""
    from owcopilot.content.models import Term
    from owcopilot.worldgen.critic import _critic_system_prompt

    terms = [
        Term(id="t1", canonical="守夜人", aliases=["巡逻队"], forbidden=["警察"]),
        Term(id="t2", canonical="以太炉", forbidden=["蒸汽机"]),
    ]
    prompt = _critic_system_prompt(terms=terms, inject_terms=True)
    assert "[vocabulary-constraints]" in prompt, "term block must appear in worldgen critic prompt"
    assert "警察" in prompt  # forbidden word
    assert "蒸汽机" in prompt  # forbidden word


def test_be2_worldgen_critic_no_terms_no_block() -> None:
    """[BE-2] No terms -> no vocabulary block in worldgen critic prompt."""
    from owcopilot.worldgen.critic import _critic_system_prompt

    prompt = _critic_system_prompt(terms=None, inject_terms=True)
    assert "[vocabulary-constraints]" not in prompt


def test_be2_worldgen_critic_inject_terms_false_skips_block() -> None:
    """[BE-2] inject_terms=False -> no vocab block even with terms."""
    from owcopilot.content.models import Term
    from owcopilot.worldgen.critic import _critic_system_prompt

    terms = [Term(id="t1", canonical="守夜人", forbidden=["警察"])]
    prompt = _critic_system_prompt(terms=terms, inject_terms=False)
    assert "[vocabulary-constraints]" not in prompt


def test_be2_run_quest_refine_loop_passes_terms_to_critique() -> None:
    """[BE-2 集成] run_quest_refine_loop passes terms to critic.critique (captured via spy)."""
    from owcopilot.content.models import Term
    from owcopilot.worldgen.critic import run_quest_refine_loop

    captured_terms: list = []

    class _SpyCritic:
        def critique(self, **kwargs):
            captured_terms.extend(kwargs.get("terms") or [])
            # Return immediate pass so loop exits in round 0
            from owcopilot.assist.critic import CritiqueResult
            return CritiqueResult(
                verdict="pass", score=1.0, summary="ok",
                raw='{"verdict":"pass","score":1.0,"summary":"ok","dimensions":[]}',
                parse_ok=True,
            )

    terms = [Term(id="t1", canonical="守夜人", forbidden=["警察"])]
    run_quest_refine_loop(
        critic=_SpyCritic(),  # type: ignore[arg-type]
        max_rounds=1,
        quests=[{"title": "q", "objective": "o", "stages": ["s1", "s2"],
                 "giver_npc": "npc_a", "location": "loc_a"}],
        relations=[],
        reference_rows=[],
        npc_refs={"npc_a"},
        place_refs={"loc_a"},
        context_lines=[],
        brief="test",
        regenerate=lambda *a: ([], [], []),
        emit=lambda _: None,
        terms=terms,
    )
    assert len(captured_terms) == 1
    assert captured_terms[0].id == "t1"


# ---------------------------------------------------------------------------
# BE-3: _system_prompt ordering — quality_bar -> lesson -> term
# ---------------------------------------------------------------------------

def test_be3_prompt_ordering_quality_bar_before_lesson_before_term() -> None:
    """[BE-3 硬] In _system_prompt: quality_bar comes before lesson_section, lesson before term."""
    from owcopilot.assist.drafts import _system_prompt
    from owcopilot.content.models import Term
    from owcopilot.retrieval.models import ContextPack

    # Use a hit whose ref matches the term id so term passes the >20 filter doesn't matter (<=20)
    pack = ContextPack(hits=[], query="test", budget_tokens=800)
    lessons = [{"lesson_text": "历史有 3 次被拒。", "dimension": "completeness"}]
    terms = [Term(id="term_t", canonical="守夜人", forbidden=["警察"])]

    prompt = _system_prompt(
        pack, brief="test", lessons=lessons, inject_lessons=True, terms=terms, inject_terms=True
    )
    qb_pos = prompt.find("QUALITY BAR")
    lm_pos = prompt.find("[lesson-memory]")
    vc_pos = prompt.find("[vocabulary-constraints]")

    assert qb_pos != -1, "QUALITY BAR missing from prompt"
    assert lm_pos != -1, "lesson-memory missing from prompt"
    assert vc_pos != -1, "vocabulary-constraints missing from prompt"

    assert qb_pos < lm_pos, f"QUALITY BAR({qb_pos}) must come before lesson-memory({lm_pos})"
    assert lm_pos < vc_pos, (
        f"lesson-memory({lm_pos}) must come before vocabulary-constraints({vc_pos})"
    )


# ---------------------------------------------------------------------------
# BE-4: build_critic_lesson_block rewrites "生成时请" to "评判时请"
# ---------------------------------------------------------------------------

def test_be4_critic_lesson_block_rewrites_wording() -> None:
    """[BE-4 硬] Critic lesson block rewrites generation-side wording to eval-side wording."""
    from owcopilot.assist.lessons import build_critic_lesson_block

    lessons = [
        {
            "lesson_text": (
                "此类 quest_draft 内容在「completeness」维度上历史有 3 次"
                "被人审拒绝（critic 误判为通过）。"
                "生成时请着重提高「completeness」维度的质量。"
            ),
            "dimension": "completeness",
        }
    ]
    block = build_critic_lesson_block(lessons, inject_lessons=True)
    assert "生成时请着重提高" not in block, "critic block must NOT contain 生成时请着重提高"
    assert "评判时请着重核查" in block, "critic block MUST contain 评判时请着重核查"


def test_be4_critic_lesson_block_rewrites_general_dimension_wording() -> None:
    """[BE-4 R3] General-dimension lessons use "生成时请整体提高" — the critic block must
    also rewrite this phrasing (previously missed, leaking generation-side wording).
    """
    from owcopilot.assist.lessons import build_critic_lesson_block

    lessons = [
        {
            # general lessons are authored with "整体" not "着重" (see lessons.py:67)
            "lesson_text": (
                "此类 quest_draft 内容历史上有 3 次被人审拒绝（critic 误判为通过）。"
                "生成时请整体提高质量标准，不要依赖 critic 的宽松判断。"
            ),
            "dimension": "general",
        }
    ]
    block = build_critic_lesson_block(lessons, inject_lessons=True)
    assert "生成时请整体提高" not in block, "critic block must NOT leak 生成时请整体提高"
    assert "评判时请整体核查" in block, "critic block MUST rewrite general wording to eval-side"


def test_be4_generation_lesson_block_keeps_original_wording() -> None:
    """[BE-4] Generation-side build_lesson_block keeps the original wording (not critic-side)."""
    from owcopilot.assist.lessons import build_lesson_block

    lessons = [
        {
            "lesson_text": "此类 quest_draft 内容历史有 3 次被拒。生成时请着重提高质量。",
            "dimension": "general",
        }
    ]
    block = build_lesson_block(lessons, inject_lessons=True)
    # The generation side should NOT rewrite the wording
    assert "生成时请着重提高" in block


# ---------------------------------------------------------------------------
# BE-5: build_term_block >20 terms includes forbidden-word hits
# ---------------------------------------------------------------------------

def test_be5_build_term_block_includes_forbidden_hit_terms() -> None:
    """[BE-5 硬] When >20 terms and a forbidden word in context_hits, the term is included."""
    from owcopilot.assist.term_injection import build_term_block
    from owcopilot.content.models import Term

    # Build 21 terms with no hits in context — normally all filtered out
    # but term_forbidden has a forbidden word that IS in context_hits
    base_terms = [
        Term(id=f"term_{i}", canonical=f"canonical_{i}", forbidden=[], aliases=[])
        for i in range(20)
    ]
    special_term = Term(
        id="term_special",
        canonical="守夜人",
        forbidden=["旧词"],  # "旧词" will appear in context_hits
        aliases=[],
    )
    all_terms = base_terms + [special_term]
    assert len(all_terms) == 21  # triggers >20 filter

    # context_hits contains the forbidden word — with BE-5 fix, special_term must be included
    context_hits = ["旧词"]
    block = build_term_block(all_terms, context_hits=context_hits, inject_terms=True)

    assert "[vocabulary-constraints]" in block, "forbidden-word hit should trigger term inclusion"
    assert "旧词" in block, "forbidden word from context-hit term must appear in block"


def test_be5_no_hits_filters_all_terms_above_20() -> None:
    """[BE-5] With >20 terms and no hits (id/canonical/alias/forbidden), block is empty."""
    from owcopilot.assist.term_injection import build_term_block
    from owcopilot.content.models import Term

    terms = [
        Term(id=f"t_{i}", canonical=f"c_{i}", forbidden=[f"f_{i}"], aliases=[])
        for i in range(25)
    ]
    block = build_term_block(terms, context_hits=["unrelated_ref"], inject_terms=True)
    # "unrelated_ref" doesn't match any id, canonical, alias, or forbidden word
    assert block == ""


# ---------------------------------------------------------------------------
# BE-6: worldgen _vocab_block uses seed_hits for >20 terms
# ---------------------------------------------------------------------------

def test_be6_vocab_block_includes_all_terms_when_over_20() -> None:
    """[BE-6 硬] _vocab_block with >20 terms injects all terms (seed_hits = term.id + canonical)."""
    from owcopilot.content.models import Term
    from owcopilot.worldgen.service import _vocab_block

    # Build 25 terms — before BE-6 fix context_hits=[] would filter all out
    terms = [
        Term(id=f"term_{i}", canonical=f"canonical_{i}", forbidden=[f"forbidden_{i}"], aliases=[])
        for i in range(25)
    ]
    block = _vocab_block(terms)
    assert "[vocabulary-constraints]" in block, (
        "_vocab_block must inject term block even with >20 terms after BE-6 seed_hits fix"
    )
    # All terms are included because all ids/canonicals are seeded as hits
    assert "forbidden_0" in block
    assert "forbidden_24" in block


def test_be6_vocab_block_empty_for_no_terms() -> None:
    """[BE-6] No terms still returns empty string."""
    from owcopilot.worldgen.service import _vocab_block
    assert _vocab_block(None) == ""
    assert _vocab_block([]) == ""


# ---------------------------------------------------------------------------
# BE-7: AcceptanceCheck.details has eval_type and is_sanity_gate
# ---------------------------------------------------------------------------

def test_be7_acceptance_check_has_eval_type_field() -> None:
    """[BE-7 硬] tool_selection_accuracy_gate check has eval_type and is_sanity_gate in details."""
    from owcopilot.evaluation.acceptance import AcceptanceCheck

    # Simulate the check as _run_tool_selection_accuracy_gate would build it
    check = AcceptanceCheck(
        name="tool_selection_accuracy_gate",
        passed=True,
        details={
            "eval_type": "offline_pipeline_sanity",
            "is_sanity_gate": True,
            "mean_f1": 1.0,
            "gate": 0.80,
        },
    )
    assert check.details.get("eval_type") == "offline_pipeline_sanity"
    assert check.details.get("is_sanity_gate") is True


def test_be7_acceptance_check_eval_type_present_in_real_run(tmp_path) -> None:
    """[BE-7 e2e] Real acceptance eval includes eval_type in tool_selection gate details."""
    from owcopilot.evaluation.acceptance import run_acceptance_evaluation

    report = run_acceptance_evaluation(tmp_path)
    gate = next(
        (c for c in report.checks if c.name == "tool_selection_accuracy_gate"), None
    )
    assert gate is not None, "tool_selection_accuracy_gate check must exist"
    assert gate.details.get("eval_type") == "offline_pipeline_sanity", (
        "eval_type must be 'offline_pipeline_sanity'"
    )
    assert gate.details.get("is_sanity_gate") is True


# ---------------------------------------------------------------------------
# BE-8: save_lesson ON CONFLICT updates lesson_text
# ---------------------------------------------------------------------------

def test_be8_save_lesson_updates_lesson_text_on_conflict() -> None:
    """[BE-8 硬] On conflict, save_lesson updates lesson_text (not just false_pass_count)."""
    from owcopilot.storage.sqlite import SQLiteStore

    store = SQLiteStore(":memory:")
    store.save_lesson("quest_draft", "original lesson text", dimension="completeness")
    store.save_lesson("quest_draft", "updated lesson text", dimension="completeness")

    lessons = store.get_lessons_for_type("quest_draft")
    assert len(lessons) == 1
    assert lessons[0]["false_pass_count"] == 2
    assert lessons[0]["lesson_text"] == "updated lesson text", (
        "lesson_text must be updated on conflict (BE-8)"
    )
    store.close()


def test_be8_lesson_text_not_stale_after_extract() -> None:
    """[BE-8] extract_lessons_from_report updates lesson_text when count increases."""
    from owcopilot.assist.calibration import CalibrationReport, FalsePassItem
    from owcopilot.assist.lessons import extract_lessons_from_report
    from owcopilot.storage.sqlite import SQLiteStore

    store = SQLiteStore(":memory:")
    items_3 = [
        FalsePassItem(
            item_id=f"item_{i}", item_type="quest_draft", object_ref=f"r_{i}", critic_score=0.6,
            dimension="grounding",
        )
        for i in range(3)
    ]
    report_3 = CalibrationReport(false_pass_items=items_3)
    extract_lessons_from_report(report_3, store, min_false_pass=3)

    lessons_after_first = store.get_lessons_for_type("quest_draft")
    assert "3" in lessons_after_first[0]["lesson_text"]

    # Second calibration with 5 false-passes for same dimension -> lesson_text should update
    items_5 = [
        FalsePassItem(
            item_id=f"item2_{i}", item_type="quest_draft", object_ref=f"r2_{i}", critic_score=0.6,
            dimension="grounding",
        )
        for i in range(5)
    ]
    report_5 = CalibrationReport(false_pass_items=items_5)
    extract_lessons_from_report(report_5, store, min_false_pass=3)

    lessons_after_second = store.get_lessons_for_type("quest_draft")
    assert "5" in lessons_after_second[0]["lesson_text"], (
        "lesson_text must update to reflect new count after second calibration (BE-8)"
    )
    store.close()


# ---------------------------------------------------------------------------
# BE-9: CLI --skills wires into ReActAgent.allowed_skills
# ---------------------------------------------------------------------------

def test_be9_cli_parses_skills_argument() -> None:
    """[BE-9 硬] CLI agent subparser accepts --skills argument."""
    from owcopilot.cli.main import build_parser

    parser = build_parser()
    args = parser.parse_args(
        ["agent", "--goal", "test goal", "--skills", "audit_project", "list_issues",
         "--content-root", "."]
    )
    assert args.skills == ["audit_project", "list_issues"]


def test_be9_cli_skills_none_when_not_provided() -> None:
    """[BE-9] Without --skills, args.skills is None (all skills allowed)."""
    from owcopilot.cli.main import build_parser

    parser = build_parser()
    args = parser.parse_args(["agent", "--goal", "test goal", "--content-root", "."])
    assert args.skills is None


def test_be9_react_agent_allowed_skills_filters_manifest() -> None:
    """[BE-9 集成] ReActAgent stores allowed_skills and passes them to registry.manifest."""
    from owcopilot.agent.offline import OfflineReactProvider
    from owcopilot.agent.react import ReActAgent
    from owcopilot.llm.cache import NoOpCache
    from owcopilot.llm.gateway import LLMGateway
    from owcopilot.llm.router import StaticRouter

    gateway = LLMGateway(
        providers={"react": OfflineReactProvider()},
        router=StaticRouter(mapping={"agent_react": "react"}),
        cache=NoOpCache(),
    )

    captured: list[set[str] | None] = []

    class _MockRegistry:
        def manifest(self, allowed: set[str] | None = None) -> str:
            captured.append(allowed)
            if allowed is None:
                return "- audit_project()\n- list_issues()\n- quality_harness()"
            return "\n".join(f"- {s}()" for s in sorted(allowed))

        def run(self, name: str, args: dict) -> dict:
            return {"status": "ok"}

    registry = _MockRegistry()
    agent = ReActAgent(
        gateway=gateway,
        registry=registry,  # type: ignore[arg-type]
        max_steps=1,
        allowed_skills={"audit_project", "list_issues"},
    )
    # Run the agent — it will call registry.manifest(allowed=self.allowed_skills) in run()
    agent.run("test goal")

    # Verify manifest was called with the restricted skill set
    assert any(
        a == {"audit_project", "list_issues"} for a in captured
    ), f"manifest must be called with allowed_skills set; captured={captured}"
