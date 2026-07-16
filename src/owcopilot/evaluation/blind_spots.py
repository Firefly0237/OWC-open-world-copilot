# 规则盲区负对照：注入确定性规则无法捕获的叙事语义变异，证明并量化审计的沉默边界。
"""Rule blind-spot negative control (the mutation-testing counterpart of ``seed_errors``).

The 25-error acceptance suite (``acceptance.py:seed_errors``) is a *positive* control: it seeds
faults the deterministic rules are built to catch and measures ``detection_rate`` (a mutation-score
analog — killed/total in mutation-testing terms). A positive control validates *sensitivity*; on
its own it lets ``detection_rate=1.0`` be read as "the audit catches everything", which is the
"painting the target around the arrow" objection this module answers.

This is the *negative* control that validates *specificity*. Following the mutation-testing
practice of repurposing mutation to synthesize negatives (Strug 2016, DOI 10.1155/2016/6589140),
``seed_blind_spots`` takes the clean acceptance world (the positive artifact) and mutates its
free-text narrative fields into genuine, editor-recognisable defects that lie OUTSIDE the
expressive power of every deterministic rule — the "equivalent / out-of-scope mutants" that no
structural test can kill (Wikipedia: Mutation testing). We then run the exact production audit and
assert it stays SILENT (zero new issues), and report ``blind_spot_rate`` = surviving / injected as
the honest boundary metric alongside ``detection_rate``.

Together the two numbers are the biology-lab pairing: ``detection_rate`` = positive control (the
audit CAN catch faults it encodes), ``blind_spot_rate`` = negative control (the audit provably
stays silent on narrative-semantic faults). The already-live ``clean_world_zero_false_positives``
gate is the third, false-positive negative control. Every deterministic rule keys off STRUCTURE
(ids, integer bounds, tag membership, placeholder sets, regex matches), never prose meaning; the
layer designed to catch these entailment/coherence defects is the opt-in
``qa_faithfulness_entailment`` LLM judge, so this catalog also documents the exact boundary handed
off from the deterministic rules to that judge.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from ..audit.models import Issue
from ..content.models import ContentBundle
from ..content.store import ContentStore
from ..llm.cache import HashingEmbedder
from ..pipeline.audit import run_full_audit
from ..pipeline.project import ProjectContext

# --- Blind-spot category slugs = the honest "type list" of what the rules provably cannot see.
PERSONALITY_DRIFT = "personality_drift"
TONE_REGISTER_INCONSISTENCY = "tone_register_inconsistency"
MOTIVATION_SELF_CONTRADICTION = "motivation_self_contradiction"
EMOTIONAL_CONTINUITY_BREAK = "emotional_continuity_break"
UNMODELED_FACT_CONTRADICTION = "unmodeled_fact_contradiction"
PROSE_SENTIMENT_VS_STRUCTURE = "prose_sentiment_vs_structure"


class BlindSpotError(BaseModel):
    """One deliberately-characterized narrative mutation the deterministic rules cannot catch.

    Mirrors ``acceptance.py:SeededError`` in spirit but points the *other* way: instead of an
    ``expected_rule`` that should fire, it carries a human-editor ``contradiction`` rationale for
    why the mutated prose is a real defect that NO rule encodes.
    """

    code: str
    category: str
    mutated_refs: list[str]
    contradiction: str
    note: str = ""


class BlindSpotReport(BaseModel):
    """Result of the blind-spot negative control.

    A valid negative control requires ``new_issues == []`` (the mutated narrative world produced no
    new structural issues), ``caught == []`` and ``blind_spot_rate == 1.0`` (every injected defect
    survived, i.e. the audit stayed silent on all of them).
    """

    blind_spot_rate: float
    injected: int
    categories: list[str]
    survived: list[str]
    caught: list[str]
    per_spot: list[dict[str, object]] = Field(default_factory=list)
    new_issues: list[str] = Field(default_factory=list)


def seed_blind_spots(clean: ContentBundle) -> tuple[ContentBundle, list[BlindSpotError]]:
    """Deep-copy ``clean`` and inject 6 narrative-semantic contradictions the rules cannot catch.

    Mirrors ``acceptance.py:seed_errors`` in shape: deep-copy, a local ``note`` helper appends a
    ``BlindSpotError``, return ``(bundle, list)``. Every mutation is a coherent, in-UI-budget,
    forbidden-term-free, injection-free soft contradiction that rides on the only free-text fields
    the model exposes (``Entity.description``, ``Quest.objective``, ``DialogueRef.text``,
    ``POI.purpose``) — the negative-control test proves each stays silent.
    """
    bundle = clean.model_copy(deep=True)
    spots: list[BlindSpotError] = []

    def note(code: str, category: str, refs: list[str], contradiction: str, text: str = "") -> None:
        spots.append(
            BlindSpotError(
                code=code,
                category=category,
                mutated_refs=refs,
                contradiction=contradiction,
                note=text,
            )
        )

    # BS01 PERSONALITY_DRIFT — a described gentle pacifist issues a kill-order in his own dialogue.
    bundle.entities["npc_r2_b"].description += "生性温和，以调解著称，从不持刃伤人。"
    bundle.dialogues["dlg_quest_r2_q2"].text = "遇上黑沙帮的人，格杀勿论，一个不留。"
    note(
        "BS01",
        PERSONALITY_DRIFT,
        ["entity:npc_r2_b", "dialogue:dlg_quest_r2_q2"],
        "人设写明生性温和、从不持刃，本人台词却下达格杀勿论的屠杀令——性格特征前后断裂；"
        "规则只核对 speaker_id 结构指向，不读台词与人设的语义一致性。",
        "description trait vs own dialogue tone",
    )

    # BS02 TONE_REGISTER_INCONSISTENCY — one quest objective drops into modern net-slang register.
    bundle.quests[
        "quest_r3_q1"
    ].objective = "老铁们冲鸭，去北望平原把烽燧巡查这波操作整明白，搞定速速回来交差，绝绝子。"
    note(
        "BS02",
        TONE_REGISTER_INCONSISTENCY,
        ["quest:quest_r3_q1"],
        "全世界行文为古典武侠语体，此任务目标突兀切换为现代网络流行语，语域/腔调严重失调；"
        "规则只检查 objective 是否非空，不评估文体一致性。",
        "objective register clashes with the classical world voice",
    )

    # BS03 MOTIVATION_SELF_CONTRADICTION — one description holds two mutually exclusive motives.
    bundle.entities[
        "npc_r5_a"
    ].description += "他毕生只为铲除黑沙帮，与之不共戴天；同时又暗中受黑沙帮供养，甘为其效死。"
    note(
        "BS03",
        MOTIVATION_SELF_CONTRADICTION,
        ["entity:npc_r5_a"],
        "同一段人物设定内并存两个互斥动机：誓死铲除某帮、又暗中受该帮供养效忠——动机自相矛盾；"
        "规则不解析动机语义，散文里的自反不会落到任何结构字段。",
        "sworn enemy and secret loyal servant of the same faction",
    )

    # BS04 EMOTIONAL_CONTINUITY_BREAK — same giver swings grief→elation across one chain, no bridge.
    bundle.dialogues["dlg_quest_r6_q1"].text = "我的挚友昨夜战死，我心如刀绞，恳请你替他完成遗愿。"
    bundle.dialogues["dlg_quest_r6_q3"].text = "今日真是痛快！你我且痛饮三百杯，纵情高歌到天明！"
    note(
        "BS04",
        EMOTIONAL_CONTINUITY_BREAK,
        ["dialogue:dlg_quest_r6_q1", "dialogue:dlg_quest_r6_q3"],
        "同一委托人（npc_r6_a）在同一任务链中先深陷挚友战死之痛，后又毫无过渡地纵情欢庆，"
        "情感连续性断裂；规则不建模情绪弧线，只校验 speaker_id/quest_id 结构指向。",
        "same speaker: mourning then celebration, no narrative bridge",
    )

    # BS05 UNMODELED_FACT_CONTRADICTION — one beacon is both derelict ruin and thriving garrison.
    bundle.entities["loc_r7_a"].description = "落霞丘陵烽燧，早已废弃多年，断壁残垣，渺无人烟。"
    bundle.quests[
        "quest_r7_q1"
    ].objective = "前往香火鼎盛、驻军云集的落霞丘陵烽燧，完成烽燧巡查并向委托人复命。"
    note(
        "BS05",
        UNMODELED_FACT_CONTRADICTION,
        ["entity:loc_r7_a", "quest:quest_r7_q1"],
        "同一地点（落霞丘陵烽燧）在地点描述中是废弃多年、渺无人烟的残垣，在任务目标中却香火鼎盛、"
        "驻军云集——世界事实自相矛盾；该'运营状态'不是任何结构字段，规则无从比对。",
        "location operational status: abandoned vs bustling",
    )

    # BS06 PROSE_SENTIMENT_VS_STRUCTURE — prose warmth contradicts the structured enemy relation.
    bundle.entities[
        "npc_r8_a"
    ].description += "他与黑沙帮众情同手足，时常把酒言欢，视之为毕生挚友。"
    note(
        "BS06",
        PROSE_SENTIMENT_VS_STRUCTURE,
        ["entity:npc_r8_a"],
        "npc_r8_a 结构上隶属铁卫军团（fac_iron），而铁卫军团与黑沙帮（fac_sand）为 enemy_of 敌对；"
        "其人物描述却把黑沙帮众写成情同手足的挚友——散文情感与关系图谱相悖；"
        "规则只校验关系图的结构一致性，不比对散文情感取向与之矛盾。",
        "iron member described as bosom friend of the enemy gang",
    )

    return bundle, spots


def audit_open_issues(
    bundle: ContentBundle, world_root: str | Path, sqlite_path: str | Path
) -> list[Issue]:
    """Save ``bundle`` to a fresh store, run the exact production audit, return its OPEN issues.

    Pins ``HashingEmbedder`` for a $0/deterministic run, matching the acceptance gate. Shared by
    ``evaluate_blind_spots`` (for the mutated world, and the clean baseline when not supplied) and
    by the negative-control's own meaningfulness test, so both reuse one production audit path.
    """
    ContentStore(world_root).save(bundle)
    project = ProjectContext.open(world_root, sqlite_path=sqlite_path, embedder=HashingEmbedder())
    try:
        result = run_full_audit(project, persist=False)
    finally:
        project.close()
    return [issue for issue in result.issues if issue.status.value == "open"]


def build_blind_spot_report(
    baseline_open: list[Issue],
    mutated_open: list[Issue],
    spots: list[BlindSpotError],
) -> BlindSpotReport:
    """Diff two audits and attribute survival/capture per injected blind spot.

    ``new_issues`` are open issues the mutated world produced that the baseline did not (keyed by
    ``(rule_code, target_ref)``). A spot is ``caught`` when some new issue's target or evidence ref
    matches one of its ``mutated_refs`` — mirroring the detection matching in ``seed_errors`` — and
    otherwise ``survived``. ``blind_spot_rate`` = survived / injected.
    """
    baseline_keys = {(issue.rule_code, issue.target_ref) for issue in baseline_open}
    new_open = [
        issue for issue in mutated_open if (issue.rule_code, issue.target_ref) not in baseline_keys
    ]
    new_issues = [f"{issue.rule_code} {issue.target_ref}" for issue in new_open]

    survived: list[str] = []
    caught: list[str] = []
    per_spot: list[dict[str, object]] = []
    for spot in spots:
        refs = set(spot.mutated_refs)
        matched = [
            f"{issue.rule_code} {issue.target_ref}"
            for issue in new_open
            if refs & _issue_refs(issue)
        ]
        (caught if matched else survived).append(spot.code)
        per_spot.append(
            {
                "code": spot.code,
                "category": spot.category,
                "mutated_refs": spot.mutated_refs,
                "contradiction": spot.contradiction,
                "caught": bool(matched),
                "matched_issues": matched,
            }
        )

    injected = len(spots)
    rate = len(survived) / injected if injected else 0.0
    return BlindSpotReport(
        blind_spot_rate=round(rate, 4),
        injected=injected,
        categories=sorted({spot.category for spot in spots}),
        survived=survived,
        caught=caught,
        per_spot=per_spot,
        new_issues=new_issues,
    )


def evaluate_blind_spots(
    clean: ContentBundle,
    workspace: str | Path,
    *,
    baseline_open_issues: list[Issue] | None = None,
) -> BlindSpotReport:
    """Run the full blind-spot negative control against a clean acceptance world.

    Seeds the 6 narrative mutations, audits the mutated world, and diffs it against the clean
    baseline. Pass ``baseline_open_issues`` (the clean world's already-computed open issues) to skip
    re-auditing the clean world; leave it ``None`` to have this function audit the clean world too,
    so the control is runnable standalone.
    """
    root = Path(workspace)
    mutated, spots = seed_blind_spots(clean)
    mutated_open = audit_open_issues(mutated, root / "blind_spot_world", root / "blind_spot.sqlite")
    if baseline_open_issues is None:
        baseline_open_issues = audit_open_issues(
            clean, root / "blind_spot_clean", root / "blind_spot_clean.sqlite"
        )
    return build_blind_spot_report(baseline_open_issues, mutated_open, spots)


def _issue_refs(issue: Issue) -> set[str]:
    refs = {issue.target_ref}
    refs |= {evidence.target_ref for evidence in issue.evidence if evidence.target_ref}
    return refs
