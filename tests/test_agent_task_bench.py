"""Offline tests for the task-level agent benchmark (``evaluation/agent_task_bench.py``).

Everything here runs at $0: the corpus/gold half is pure and deterministic, the scoring half is
pure string arithmetic, and the end-to-end run drives the LangChain fake instead of a real model.
The point of these tests is the *scoring logic* — a benchmark whose scorer is wrong produces
confident, wrong numbers, which is worse than no benchmark.
"""

from __future__ import annotations

import pytest

from owcopilot.audit.default_rules import build_default_rule_registry
from owcopilot.evaluation.agent_task_bench import (
    AGENT_TASK_GOAL,
    FINDINGS_MARKER,
    PROMPT_VARIANTS,
    RECALL_SUCCESS_THRESHOLD,
    RETRIEVAL_ONLY_SKILLS,
    RUNGS,
    answer_follows_output_contract,
    build_agent_task_goal,
    build_task_base_world,
    build_task_worlds,
    extract_claims,
    gold_findings,
    run_agent_task_bench,
    rung_spec,
    score_answer,
)
from owcopilot.evaluation.agent_task_bench import (
    _world_identifiers as world_identifiers,
)
from owcopilot.fakes import scripted_chat_model

RULE_CODES = set(build_default_rule_registry().codes())


def _render_perfect_answer(gold) -> str:
    """A report that names every gold finding, in PROSE — i.e. without the output contract's
    declared list. Scoring it exercises the fallback path that a non-compliant answer takes."""
    return "\n".join(
        f"- {finding.rule_code} on {finding.identifiers[0]}: needs a fix"
        for finding in gold
    )


def _render_contract_answer(gold, *, notes: str = "") -> str:
    """A report in the shape the output contract asks for: working notes, marker, one line each."""
    lines = [notes or "Working notes: ran the checks and confirmed each finding below.", ""]
    lines.append(FINDINGS_MARKER)
    lines += [f"{f.rule_code} | {f.identifiers[0]} | needs a fix" for f in gold]
    return "\n".join(lines)


# --- corpus + ground truth -------------------------------------------------------------------


def test_base_world_audits_to_zero_open_findings() -> None:
    """The gold set is only attributable to the seeded defects if the base world is clean."""
    assert gold_findings(build_task_base_world()) == []


def test_task_corpus_is_diverse_and_seeds_what_it_claims() -> None:
    worlds = build_task_worlds()
    defect_worlds = [(task, bundle) for task, bundle in worlds if task.seeded]
    assert len(defect_worlds) >= 8

    all_gold_codes: set[str] = set()
    for task, bundle in defect_worlds:
        gold = gold_findings(bundle)
        assert gold, f"{task.task_id} seeded no detectable defect"
        gold_codes = {finding.rule_code for finding in gold}
        seeded_codes = {seed.expected_rule for seed in task.seeded}
        # Every seeded defect must actually be detected; extra codes are legitimate cascades.
        assert seeded_codes <= gold_codes, f"{task.task_id} missed {seeded_codes - gold_codes}"
        all_gold_codes |= gold_codes
        # Deduped on (rule_code, target_ref), and every finding is addressable by some id.
        assert len({finding.key for finding in gold}) == len(gold)
        assert all(finding.identifiers for finding in gold)

    # The whole point of a corpus is coverage: the six defect kinds the bench was built around
    # (dangling jump, unreachable node, undeclared variable, missing localization key, term
    # inconsistency, gating gap) must each be present somewhere.
    assert {
        "DIALOGUE_TREE_BROKEN_LINK",
        "DIALOGUE_TREE_UNREACHABLE_NODE",
        "DIALOGUE_CONDITION_UNDEFINED_VAR",
        "MISSING_LOCALIZATION_KEY",
        "TERM_INCONSISTENT",
        "DIALOGUE_UNGATED_SIBLING_CHOICE",
    } <= all_gold_codes


def test_gold_identifiers_exclude_schema_words() -> None:
    """A schema field name must never count as "naming the affected object" — that would let an
    answer match a finding it never localized, silently inflating recall."""
    _task, bundle = next(t for t in build_task_worlds() if t[0].task_id == "T01")
    gold = gold_findings(bundle)
    broken = next(f for f in gold if f.rule_code == "DIALOGUE_TREE_BROKEN_LINK")
    assert "dtree_r1_q1" in broken.identifiers
    assert "next_node" not in broken.identifiers  # a Quest/DialogueNode field, not an object
    assert "dialogue_tree" not in broken.identifiers  # a ref-kind prefix, not an object


def test_corpus_carries_a_clean_negative_control() -> None:
    """Without a defect-free world the benchmark cannot tell reporting apart from reciting."""
    control = [(task, bundle) for task, bundle in build_task_worlds() if not task.seeded]
    assert len(control) == 1
    _task, bundle = control[0]
    assert gold_findings(bundle) == []


# --- scoring ---------------------------------------------------------------------------------


def test_clean_world_passes_only_when_nothing_is_claimed() -> None:
    _task, bundle = next(t for t in build_task_worlds() if not t[0].seeded)
    ids = world_identifiers(bundle)

    clean_report = score_answer(
        "I ran the audit; it returned zero open issues. No consistency problems found.",
        [],
        world_identifiers=ids,
        rule_codes=RULE_CODES,
    )
    assert clean_report.success is True
    assert clean_report.fabricated_codes == []

    invented = score_answer(
        "- PREREQ_MISSING on quest_r1_q1: the prerequisite is gone",
        [],
        world_identifiers=ids,
        rule_codes=RULE_CODES,
    )
    assert invented.success is False
    assert invented.fabricated_codes == ["PREREQ_MISSING"]
    assert invented.false_pairs == ["PREREQ_MISSING@quest_r1_q1"]


def test_perfect_report_scores_full_recall_and_precision() -> None:
    _task, bundle = next(t for t in build_task_worlds() if t[0].task_id == "T08")
    gold = gold_findings(bundle)
    score = score_answer(
        _render_perfect_answer(gold),
        gold,
        world_identifiers=world_identifiers(bundle),
        rule_codes=RULE_CODES,
    )
    assert score.recall_pair == 1.0
    assert score.recall_code == 1.0
    assert score.precision_pair == 1.0
    assert score.missed_pairs == []
    assert score.success is True


def test_fabricated_finding_costs_precision_not_recall() -> None:
    """A hallucinated finding — a real rule code pinned on an object that does not have it — must
    show up as a precision loss. This is the property that stops "list every rule you know"."""
    _task, bundle = next(t for t in build_task_worlds() if t[0].task_id == "T08")
    gold = gold_findings(bundle)
    answer = _render_perfect_answer(gold) + "\n- PROMPT_INJECTION on poi_r5_b: suspicious text"
    score = score_answer(
        answer, gold, world_identifiers=world_identifiers(bundle), rule_codes=RULE_CODES
    )
    assert score.recall_pair == 1.0
    assert score.precision_pair is not None and score.precision_pair < 1.0
    assert "PROMPT_INJECTION@poi_r5_b" in score.false_pairs
    assert score.fabricated_codes == ["PROMPT_INJECTION"]


def test_a_finding_restated_via_a_different_accepted_identifier_is_one_claim_not_two() -> None:
    """``gold_findings`` dedupes several raw audit issues onto ONE ``GoldFinding`` keyed by
    ``(rule_code, target_ref)``; pre-fix, the claim side never got the same treatment -- precision
    was counted over raw ``(rule_code, identifier)`` pairs, and one finding accepts SEVERAL
    identifiers. A finding named once in the body and again -- via a DIFFERENT accepted identifier
    -- in a closing recap used to inflate the claim count by a "claim" that is not a second
    finding: a set-vs-multiset mismatch against the gold side of the same fraction.

    The fix folds a claimed pair onto its gold finding's own key before counting, so restating one
    finding through any of its accepted identifiers is one claim, not one per identifier used.
    """
    _task, bundle = next(t for t in build_task_worlds() if t[0].task_id == "T01")
    gold = gold_findings(bundle)
    broken_link = next(f for f in gold if f.rule_code == "DIALOGUE_TREE_BROKEN_LINK")
    assert "dtree_r1_q1" in broken_link.identifiers and "n_vanished" in broken_link.identifiers

    answer = (
        "- DIALOGUE_TREE_BROKEN_LINK on dtree_r1_q1: the link is dangling\n"
        "- DIALOGUE_TREE_UNREACHABLE_NODE on dtree_r2_q1: node is unreachable\n"
        "- PROMPT_INJECTION on dtree_r2_q1: suspicious content\n"
        "\n"
        "Summary: DIALOGUE_TREE_BROKEN_LINK also affects n_vanished."
    )
    score = score_answer(
        answer, gold, world_identifiers=world_identifiers(bundle), rule_codes=RULE_CODES
    )
    assert score.recall_pair == 1.0  # both real findings are still found -- recall is untouched
    # 3 distinct claims after folding: the two real findings once each, plus the one fabrication.
    # Pre-fix this scored 3/4 = 0.75 -- the recap's "n_vanished" counted as a THIRD claim on top
    # of the two real findings and the one fabrication; folding it onto
    # DIALOGUE_TREE_BROKEN_LINK's own (rule_code, target_ref) key collapses it into the claim
    # already made via "dtree_r1_q1", dropping the claim count to 3 and the fabrication's share
    # of it to 1/3.
    assert score.precision_pair == round(2 / 3, 4)
    assert score.false_pairs == ["PROMPT_INJECTION@dtree_r2_q1"]


def test_prose_without_a_rule_code_is_scored_as_a_miss() -> None:
    """The matcher is conservative by construction: a finding described only in prose cannot be
    machine-verified, so it does not count. This biases recall DOWN, never up."""
    _task, bundle = next(t for t in build_task_worlds() if t[0].task_id == "T08")
    gold = gold_findings(bundle)
    answer = "区域 region_03 的等级区间写反了，poi_r5_b 也没有叙事用途。"
    score = score_answer(
        answer, gold, world_identifiers=world_identifiers(bundle), rule_codes=RULE_CODES
    )
    assert score.recall_pair == 0.0
    assert score.recall_code == 0.0
    assert score.precision_pair is None  # claimed nothing machine-checkable


def test_rule_code_without_an_object_id_counts_only_for_the_loose_recall() -> None:
    _task, bundle = next(t for t in build_task_worlds() if t[0].task_id == "T08")
    gold = gold_findings(bundle)
    answer = "\n".join(f"- {finding.rule_code} was detected" for finding in gold)
    score = score_answer(
        answer, gold, world_identifiers=world_identifiers(bundle), rule_codes=RULE_CODES
    )
    assert score.recall_pair == 0.0  # never localized the object
    assert score.recall_code == 1.0  # but every code was named
    assert score.success is False


def test_success_threshold_is_the_documented_one() -> None:
    _task, bundle = next(t for t in build_task_worlds() if t[0].task_id == "T08")
    gold = gold_findings(bundle)  # 4 findings; reporting 3 of them = 0.75 < 0.8
    partial = _render_perfect_answer(gold[:3])
    score = score_answer(
        partial, gold, world_identifiers=world_identifiers(bundle), rule_codes=RULE_CODES
    )
    assert score.recall_pair == 0.75
    assert score.success is (0.75 >= RECALL_SUCCESS_THRESHOLD)
    assert score.success is False


def test_extract_claims_ignores_invented_codes_and_respects_token_boundaries() -> None:
    pairs, codes = extract_claims(
        "- TOTALLY_MADE_UP on quest_r1_q1\n- PREREQ_MISSING on quest_r1_q10x",
        rule_codes=RULE_CODES,
        identifiers={"quest_r1_q1", "quest_r1_q10"},
    )
    assert "TOTALLY_MADE_UP" not in codes  # not a real registry code -> not a claim
    assert codes == {"PREREQ_MISSING"}
    # quest_r1_q10x must NOT match quest_r1_q1 or quest_r1_q10 (boundary-safe matching).
    assert pairs == set()


def test_segments_keep_findings_apart() -> None:
    """Two findings on two lines must not cross-pair into four claims."""
    pairs, _codes = extract_claims(
        "- PREREQ_MISSING on quest_r1_q1\n- TIMELINE_VIOLATION on quest_r2_q1",
        rule_codes=RULE_CODES,
        identifiers={"quest_r1_q1", "quest_r2_q1"},
    )
    assert pairs == {
        ("PREREQ_MISSING", "quest_r1_q1"),
        ("TIMELINE_VIOLATION", "quest_r2_q1"),
    }


def test_multiline_markdown_finding_is_matched() -> None:
    """Regression pin from the first live run: DeepSeek reports the rule code on the heading line
    and the affected object on the NEXT line. A per-line matcher scored that perfect report as a
    total miss, so segments are blocks (paragraph / list item), not lines."""
    _task, bundle = next(t for t in build_task_worlds() if t[0].task_id == "T01")
    gold = gold_findings(bundle)
    answer = (
        "## Consistency Problems Found (2 issues, both verified)\n\n"
        "**1. ERROR — DIALOGUE_TREE_BROKEN_LINK**  \n"
        "Affected object: `dtree_r1_q1`  \n"
        "Details: the link at `nodes/n_lie/next_node` points to `n_vanished`, "
        "a node that does not exist.\n\n"
        "**2. WARNING — DIALOGUE_TREE_UNREACHABLE_NODE**  \n"
        "Affected object: `dtree_r2_q1`  \n"
        "Details: node `n_orphan` is unreachable from the root node.\n"
    )
    score = score_answer(
        answer, gold, world_identifiers=world_identifiers(bundle), rule_codes=RULE_CODES
    )
    assert score.recall_pair == 1.0
    assert score.precision_pair == 1.0
    assert score.success is True


def test_markdown_table_rows_are_separate_findings() -> None:
    """Second regression pin from the live runs, and a scorer DEFECT rather than an agent one.

    A model that lays its findings out as a markdown table writes the rows back-to-back — no blank
    line, no list marker. Splitting on paragraphs/list items alone glued the whole table into one
    segment and cross-multiplied every code by every id in it, scoring a fully correct 3-finding
    table at 0.33 precision. One table row is one finding, so it must be one segment.
    """
    _task, bundle = next(t for t in build_task_worlds() if t[0].task_id == "T08")
    gold = gold_findings(bundle)
    header = "| # | Rule Code | Target |\n|---|---|---|\n"
    rows = "".join(
        f"| {n} | `{finding.rule_code}` | `{finding.identifiers[0]}` |\n"
        for n, finding in enumerate(gold, start=1)
    )
    score = score_answer(
        header + rows, gold, world_identifiers=world_identifiers(bundle), rule_codes=RULE_CODES
    )
    assert score.recall_pair == 1.0
    assert score.precision_pair == 1.0  # was 1/len(gold) while the table was one segment
    assert score.false_pairs == []


# --- output contract -------------------------------------------------------------------------


def test_the_declared_list_is_read_instead_of_the_prose_around_it() -> None:
    """The root fix for the precision artefact: a closing recap is ONE sentence but used to
    cross-multiply into several claims, because the scorer had to guess which prose was the report.

    With the contract the agent says so itself. The working notes below name real findings AND a
    rule code that does not apply here; only the declared list counts, so the notes cost nothing.
    """
    _task, bundle = next(t for t in build_task_worlds() if t[0].task_id == "T01")
    gold = gold_findings(bundle)
    answer = (
        "I retrieved dtree_r1_q1 and dtree_r2_q1 and checked their links.\n"
        "Safe next step: re-check DIALOGUE_TREE_BROKEN_LINK, PROMPT_INJECTION and "
        "DIALOGUE_TREE_UNREACHABLE_NODE against dtree_r1_q1, dtree_r2_q1 and n_vanished "
        "before shipping.\n"
        "\n" + FINDINGS_MARKER + "\n"
        "DIALOGUE_TREE_BROKEN_LINK | dtree_r1_q1 | n_lie jumps to a node that does not exist\n"
        "DIALOGUE_TREE_UNREACHABLE_NODE | dtree_r2_q1 | n_orphan is unreachable from the root\n"
    )
    score = score_answer(
        answer, gold, world_identifiers=world_identifiers(bundle), rule_codes=RULE_CODES
    )
    assert score.contract_followed is True
    assert score.recall_pair == 1.0
    assert score.precision_pair == 1.0
    assert score.false_pairs == []
    # The recap named PROMPT_INJECTION, which is not a finding here. Under the prose fallback that
    # is a fabricated code; inside the contract it is working notes and costs nothing.
    assert score.fabricated_codes == []


def test_an_answer_without_the_marker_still_scores_through_the_prose_fallback() -> None:
    """The contract must not be a gate: a non-compliant answer is scored on what it says, exactly
    as before, so a rung whose model ignores the format is not silently zeroed."""
    _task, bundle = next(t for t in build_task_worlds() if t[0].task_id == "T08")
    gold = gold_findings(bundle)
    score = score_answer(
        _render_perfect_answer(gold),
        gold,
        world_identifiers=world_identifiers(bundle),
        rule_codes=RULE_CODES,
    )
    assert score.contract_followed is False
    assert score.recall_pair == 1.0
    assert score.precision_pair == 1.0


def test_an_empty_declared_list_is_a_clean_report_on_a_clean_world() -> None:
    """The negative-control failure the contract fixes at the source: an answer that lists every
    code it checked and found NOTHING for used to read, to a co-occurrence extractor, as 34
    fabrications. The declared list says which of them are accusations — none."""
    _task, bundle = next(t for t in build_task_worlds() if not t[0].seeded)
    answer = (
        "I ran every check. None of CHARACTER_STATE_CONTRADICTION, PREREQ_CYCLE, "
        "PROMPT_INJECTION or MISSING_LOCALIZATION_KEY fired on quest_r1_q1 or npc_r1_a.\n"
        "\n" + FINDINGS_MARKER + "\nNONE\n"
    )
    score = score_answer(
        answer, [], world_identifiers=world_identifiers(bundle), rule_codes=RULE_CODES
    )
    assert score.contract_followed is True
    assert score.fabricated_codes == []
    assert score.false_pairs == []
    assert score.success is True


def test_a_fabrication_inside_the_declared_list_still_costs_precision() -> None:
    """The contract changes which text is the report, NOT how a report is judged. Moving a made-up
    finding into the declared list must not launder it."""
    _task, bundle = next(t for t in build_task_worlds() if t[0].task_id == "T08")
    gold = gold_findings(bundle)
    answer = _render_contract_answer(gold) + "\nPROMPT_INJECTION | poi_r5_b | suspicious text\n"
    score = score_answer(
        answer, gold, world_identifiers=world_identifiers(bundle), rule_codes=RULE_CODES
    )
    assert score.recall_pair == 1.0
    assert score.precision_pair == round(len(gold) / (len(gold) + 1), 4)
    assert score.false_pairs == ["PROMPT_INJECTION@poi_r5_b"]
    assert score.fabricated_codes == ["PROMPT_INJECTION"]


def test_only_the_last_marker_opens_the_list() -> None:
    """A model that announces the format before using it must not truncate its own report."""
    assert answer_follows_output_contract("no list here") is False
    answer = (
        f"I will finish with a {FINDINGS_MARKER} block as instructed.\n"
        "\n" + FINDINGS_MARKER + "\nPREREQ_CYCLE | quest_r7_q1 | cycle\n"
    )
    pairs, codes = extract_claims(
        answer, rule_codes=RULE_CODES, identifiers={"quest_r7_q1", "quest_r7_q2"}
    )
    assert pairs == {("PREREQ_CYCLE", "quest_r7_q1")}
    assert codes == {"PREREQ_CYCLE"}


# --- the ablation ladder ---------------------------------------------------------------------


def test_each_rung_adds_exactly_one_variable_to_the_one_below() -> None:
    """The ladder's only claim is monotone single-variable steps; if two things change at once, a
    rung-to-rung gap is no longer attributable and the whole curve is decorative."""
    specs = [rung_spec(rung) for rung in RUNGS]
    assert [s.rung for s in specs] == ["L0", "L1", "L2", "L3", "L4"]
    for lower, upper in zip(specs, specs[1:], strict=False):
        changed = sum(
            (
                lower.allowed_skills != upper.allowed_skills,
                lower.prompt_variant != upper.prompt_variant,
                lower.dialogue_trees_readable != upper.dialogue_trees_readable,
            )
        )
        assert changed == 1, f"{lower.rung} -> {upper.rung} changed {changed} variables"
        # And no rung ever takes a capability away.
        if lower.allowed_skills is not None and upper.allowed_skills is not None:
            assert lower.allowed_skills <= upper.allowed_skills
        assert lower.dialogue_trees_readable <= upper.dialogue_trees_readable
    assert specs[0].allowed_skills == frozenset()  # L0 = no tools at all
    assert specs[-1].allowed_skills is None  # L4 = the unrestricted production surface
    with pytest.raises(ValueError, match="未知的梯级"):
        rung_spec("L9")


def test_no_rung_below_the_top_can_reach_the_audit_through_any_skill() -> None:
    """The removal must close every route to the gold set, not just the obvious two.

    ``quality_harness`` runs the SAME deterministic audit internally and returns the same issues,
    so leaving it in would have made every lower rung a relabelled L4. ``propose_fix`` needs an
    issue_id the audit mints. This test is the guard against a future skill quietly re-opening it.
    """
    from owcopilot.skills import default_skill_registry

    production = set(default_skill_registry(content_root="unused").names())
    for rung in RUNGS[:-1]:
        allowed = rung_spec(rung).allowed_skills
        assert allowed is not None
        assert {"audit_project", "list_issues", "quality_harness", "propose_fix"} & allowed == set()
        # The allow-list must name only skills that really exist, or the rung is silently emptier
        # than it claims.
        assert allowed <= production
    assert RETRIEVAL_ONLY_SKILLS <= production


def test_dialogue_trees_are_in_the_read_surface_after_the_fix() -> None:
    """The inverse of the test that used to pin this as a limitation.

    Dialogue trees were indexed by neither the retrieval index (``_content_rows``) nor the content
    graph, so no retrieval-only rung could ever see one and 24 gold findings were structurally
    invisible. That was a product defect, not an evaluation parameter, and L3 is the rung that
    measures fixing it. Both surfaces must carry the tree AND its structure — a row holding only
    the spoken lines would look indexed while still hiding every structural defect.
    """
    from owcopilot.graph.index import build_content_graph
    from owcopilot.storage.sqlite import _content_rows

    bundle = build_task_base_world()
    rows = {ref: body for ref, _t, _i, _title, body in _content_rows(bundle)}
    assert "dialogue_tree:dtree_r1_q1" in rows
    body = rows["dialogue_tree:dtree_r1_q1"]
    for token in ("quest_id=quest_r1_q1", "root_node=n_open", "n_lie", "told_truth"):
        assert token in body
    graph = build_content_graph(bundle)
    assert "dialogue_tree:dtree_r1_q1" in graph.node_refs()
    neighbours = graph.neighbors("dialogue_tree:dtree_r1_q1", radius=1)
    assert "quest:quest_r1_q1" in neighbours
    assert "entity:npc_r1_a" in neighbours


def test_rungs_below_l3_reproduce_the_pre_fix_read_surface(tmp_path) -> None:
    """L3's delta is only interpretable if the rungs under it really cannot see a tree.

    The reproduction is a bundle with no dialogue trees, which is exact *because* those rungs have
    no audit — nothing else on them reads the bundle. The gold set is unaffected: it is computed
    from the full world, so the 24 tree findings stay in the denominator and count as misses.
    """
    from owcopilot.evaluation.agent_task_bench import _readable_bundle

    _task, bundle = next(t for t in build_task_worlds() if t[0].task_id == "T01")
    hidden = _readable_bundle(bundle, dialogue_trees_readable=False)
    assert hidden.dialogue_trees == {}
    assert bundle.dialogue_trees  # the ablation copies; it must not mutate the world
    assert _readable_bundle(bundle, dialogue_trees_readable=True) is bundle
    # The gold set still contains the dialogue-tree findings the hidden surface cannot show.
    assert [f for f in gold_findings(bundle) if f.target_ref.startswith("dialogue_tree:")]

    report = run_agent_task_bench(
        tmp_path / "ws",
        chat_model_factory=lambda: scripted_chat_model(
            [("build_context_pack", {"query": "dialogue"}), "nothing found"]
        ),
        llm_mode="offline",
        trials=1,
        max_steps=3,
        max_replans=0,
        task_ids=["T01"],
        rung="L1",
    )
    assert report.dialogue_trees_readable is False
    from owcopilot.content.store import ContentStore

    saved = ContentStore(tmp_path / "ws" / "T01").load()
    assert saved.dialogue_trees == {}
    assert report.tasks[0].gold_total == 2  # both of them tree findings, both unreachable
    assert report.tasks[0].trials[0].score.recall_pair == 0.0


def test_a_hidden_tree_surface_can_never_be_combined_with_the_audit() -> None:
    """The audit reads the bundle directly, so hiding trees from an audit-capable rung would drop
    24 gold findings and report an artefact. Structurally impossible in the ladder; this pins that
    an override cannot reintroduce it either."""
    from owcopilot.evaluation.agent_task_bench import _validate_rung_surface

    hidden_rungs = [rung_spec(r) for r in RUNGS if not rung_spec(r).dialogue_trees_readable]
    assert hidden_rungs  # the ladder really does have such rungs
    for spec in hidden_rungs:
        assert spec.allowed_skills is not None and "audit_project" not in spec.allowed_skills
        with pytest.raises(ValueError, match="审计直接读 bundle"):
            _validate_rung_surface(spec, None)


# --- prompt variants -------------------------------------------------------------------------


def test_every_variant_shares_the_task_the_vocabulary_and_the_contract() -> None:
    """A prompt A/B is only about method if everything else is byte-identical between variants."""
    goals = {variant: build_agent_task_goal(variant) for variant in PROMPT_VARIANTS}
    for variant, goal in goals.items():
        for code in ("DIALOGUE_TREE_BROKEN_LINK", "PREREQ_CYCLE", "TERM_INCONSISTENT"):
            assert code in goal, variant
        # Codes no task seeds are listed too: a "recite the checklist" answer must cost precision.
        assert "UNREVIEWED_AI_CONTENT" in goal, variant
        assert FINDINGS_MARKER in goal, variant
        assert "Output contract" in goal, variant
    assert goals["base"] == AGENT_TASK_GOAL
    with pytest.raises(ValueError, match="未知的提示词变体"):
        build_agent_task_goal("no_such_variant")


def test_no_variant_leaks_anything_about_the_seeded_defects() -> None:
    """The variants may say HOW to look, never WHAT is there.

    The baseline goal names two real ids (``quest_r1_q1`` / ``dtree_r1_q1``) as a *format* example
    — "write the id like this" — and has done since the archived protocol, so it is a disclosed
    condition shared by every rung rather than a differentiator. What must hold is that no VARIANT
    adds an id of its own: a variant that named an affected object, or a dangling id a defect
    introduces, would be handing over part of the answer under the guise of method advice.
    """
    world_ids: set[str] = set()
    for _task, bundle in build_task_worlds():
        world_ids |= world_identifiers(bundle)
    # Ids that exist ONLY as the dangling references a seeded defect creates: naming any of these
    # would point straight at a defect.
    world_ids |= {"n_vanished", "npc_phantom_77", "bribe_paid", "npc_missing_01"}
    baseline = {ident for ident in world_ids if ident in AGENT_TASK_GOAL}
    assert baseline == {"quest_r1_q1", "dtree_r1_q1"}  # the documented format example, nothing more
    for variant in PROMPT_VARIANTS:
        leaked = sorted(
            ident for ident in world_ids - baseline if ident in build_agent_task_goal(variant)
        )
        assert leaked == [], f"variant {variant} leaked {leaked}"


# --- end-to-end harness (offline double) -----------------------------------------------------


def test_bench_runs_end_to_end_offline(tmp_path) -> None:
    """Drive the whole harness with a LangChain fake: one audit call, then a canned report that
    names every gold finding of T10. Proves the wiring (world save, project/session, telemetry,
    reliability aggregation) without spending a cent."""
    _task, bundle = next(t for t in build_task_worlds() if t[0].task_id == "T10")
    perfect = _render_perfect_answer(gold_findings(bundle))

    report = run_agent_task_bench(
        tmp_path / "ws",
        chat_model_factory=lambda: scripted_chat_model(
            [("audit_project", {}), perfect]
        ),
        llm_mode="offline",
        model="fake",
        trials=2,
        max_steps=4,
        max_replans=0,
        task_ids=["T10"],
    )

    assert report.n_tasks == 1
    assert report.trials_per_task == 2
    task_report = report.tasks[0]
    assert task_report.n_trials == 2
    assert [record.tool_calls for record in task_report.trials] == [["audit_project"]] * 2
    assert task_report.mean_recall_pair == 1.0
    assert task_report.successes == 2
    # k=2 trials per task with both successful ⇒ a genuine pass^2 of 1.0 (not pass@1 relabelled).
    assert report.reliability.max_trials == 2
    assert report.reliability.pass_hat_k[2] == 1.0
    assert report.goal == build_agent_task_goal(rung_spec(report.rung).prompt_variant)
    assert report.budget_stopped is False
    assert task_report.is_negative_control is False


def test_a_lower_rung_denies_the_audit_at_execution_not_just_in_the_manifest(tmp_path) -> None:
    """The removal has to bite at ``run()``. A model that calls ``audit_project`` anyway — because
    it read the name somewhere, or because an observation told it to — must get an error
    observation, not the gold set. Hiding the tool from the manifest alone would leave every lower
    rung a relabelled L4 whenever the model guessed the name."""
    report = run_agent_task_bench(
        tmp_path / "ws",
        chat_model_factory=lambda: scripted_chat_model(
            [("audit_project", {}), ("build_context_pack", {"query": "quests"}), "nothing found"]
        ),
        llm_mode="offline",
        trials=1,
        max_steps=4,
        max_replans=0,
        task_ids=["T10"],
        rung="L1",
    )
    assert report.rung == "L1"
    assert "audit_project" not in report.allowed_skills
    record = report.tasks[0].trials[0]
    assert record.tool_calls[0] == "audit_project"  # it did try
    assert record.score.recall_pair == 0.0  # and learned nothing from it
    assert report.tasks[0].successes == 0


def test_l0_runs_with_no_tools_at_all(tmp_path) -> None:
    """L0 is the ladder's floor: the model sees the goal and nothing else. A tool call there is not
    merely unlisted, it is unroutable — otherwise L0 would quietly be L1."""
    report = run_agent_task_bench(
        tmp_path / "ws",
        chat_model_factory=lambda: scripted_chat_model(
            [("build_context_pack", {"query": "quests"}), "I had no tools; here is my best guess."]
        ),
        llm_mode="offline",
        trials=1,
        max_steps=3,
        max_replans=0,
        task_ids=["T10"],
        rung="L0",
    )
    assert report.rung == "L0"
    assert report.allowed_skills == []
    record = report.tasks[0].trials[0]
    assert record.tool_calls == ["build_context_pack"]  # it tried
    assert record.score.recall_pair == 0.0  # and got an error observation, not content


def test_the_top_rung_keeps_the_full_production_surface(tmp_path) -> None:
    _task, bundle = next(t for t in build_task_worlds() if t[0].task_id == "T10")
    report = run_agent_task_bench(
        tmp_path / "ws",
        chat_model_factory=lambda: scripted_chat_model(
            [("audit_project", {}), _render_contract_answer(gold_findings(bundle))]
        ),
        llm_mode="offline",
        trials=1,
        max_steps=4,
        max_replans=0,
        task_ids=["T10"],
        rung="L4",
    )
    assert report.rung == "L4"
    assert report.allowed_skills == ["<full production surface>"]
    assert report.dialogue_trees_readable is True
    assert report.tasks[0].trials[0].score.recall_pair == 1.0
    assert report.contract_followed_rate == 1.0


def test_a_prompt_variant_override_is_recorded_not_implied(tmp_path) -> None:
    """The prompt A/B sweeps variants at a fixed rung, so an overridden run must be distinguishable
    from the ladder's own rung in the archived JSON — not only in whoever's notes."""
    report = run_agent_task_bench(
        tmp_path / "ws",
        chat_model_factory=lambda: scripted_chat_model(["nothing found"]),
        llm_mode="offline",
        trials=1,
        max_steps=2,
        max_replans=0,
        task_ids=["T10"],
        rung="L1",
        prompt_variant="checklist",
    )
    assert report.prompt_variant == "checklist"
    assert report.goal == build_agent_task_goal("checklist")
    assert report.goal != AGENT_TASK_GOAL


def test_negative_control_is_excluded_from_the_pooled_recall_mean(tmp_path) -> None:
    """A clean world's recall is vacuously 1.0; averaging it into the headline would inflate a
    number that measures nothing. It must still be reported, and still count toward pass^k."""
    report = run_agent_task_bench(
        tmp_path / "ws",
        chat_model_factory=lambda: scripted_chat_model(
            [("audit_project", {}), "The audit returned zero open issues; nothing to report."]
        ),
        llm_mode="offline",
        trials=1,
        max_steps=3,
        max_replans=0,
        task_ids=["T00"],
    )
    control = report.tasks[0]
    assert control.is_negative_control is True
    assert control.gold_total == 0
    assert control.successes == 1  # claimed nothing on a clean world
    assert report.mean_recall_pair == 0.0  # no defect world contributed -> empty mean, not 1.0
    assert report.reliability.trials == 1  # but the control DOES count as a trial


def test_unknown_task_id_fails_loudly(tmp_path) -> None:
    """A typo'd --task must not silently produce an empty (and thus meaningless) report."""
    with pytest.raises(ValueError, match="未知的任务 id"):
        run_agent_task_bench(
            tmp_path / "ws",
            chat_model_factory=lambda: scripted_chat_model(["done"]),
            llm_mode="offline",
            trials=1,
            task_ids=["T99"],
        )


def test_bench_stops_at_the_cost_budget(tmp_path) -> None:
    """The budget guard must be a hard stop that is REPORTED, not a silent overspend."""
    report = run_agent_task_bench(
        tmp_path / "ws",
        chat_model_factory=lambda: scripted_chat_model(["done"]),
        llm_mode="offline",
        trials=1,
        max_steps=2,
        max_replans=0,
        budget_usd=0.0,  # already at budget before the first trial
        task_ids=["T10", "T01"],
    )
    assert report.budget_stopped is True
    assert all(task.n_trials == 0 for task in report.tasks)


def test_a_crashing_trial_is_recorded_not_swallowed(tmp_path) -> None:
    """One broken trial must degrade to a zero-score record with the error text, so a partial run
    is still honest about what happened instead of vanishing."""

    def boom():
        raise RuntimeError("provider exploded")

    report = run_agent_task_bench(
        tmp_path / "ws",
        chat_model_factory=boom,
        llm_mode="offline",
        trials=1,
        max_steps=2,
        max_replans=0,
        task_ids=["T10"],
    )
    record = report.tasks[0].trials[0]
    assert record.stop_reason == "error"
    assert "provider exploded" in record.error
    assert record.score.recall_pair == 0.0
    assert report.tasks[0].successes == 0
