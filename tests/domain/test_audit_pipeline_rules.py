from __future__ import annotations

import re
from pathlib import Path

from owcopilot.audit.context import AuditContext
from owcopilot.audit.rules.pipeline_rules import (
    MissingLocalizationKeyRule,
    PlaceholderMismatchRule,
    QuestMissingObjectiveRule,
    TermInconsistentRule,
    TextTooLongForUIRule,
)
from owcopilot.audit.source_location import ContentSourceLocator
from owcopilot.content.models import ContentBundle, DialogueRef, LocalizedText, Quest, Term
from owcopilot.content.store import ContentStore

# 中文字符探测：六要素改造要求"原因"用策划语言（中文），不是英文技术描述。
_HAS_CJK = re.compile(r"[一-鿿]")


def test_missing_localization_key_rule_flags_quests_without_keys() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(quests={"quest_1": Quest(id="quest_1", title="Quest")})
    )

    issues = list(MissingLocalizationKeyRule().check(ctx))

    assert len(issues) == 1
    assert issues[0].rule_code == "MISSING_LOCALIZATION_KEY"
    assert issues[0].evidence[0].path == "localization_keys"


def test_text_too_long_for_ui_rule_flags_dialogue_text() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            dialogues={
                "dlg_long": DialogueRef(
                    id="dlg_long",
                    text_key="dlg.long",
                    text="This text is too long",
                )
            }
        )
    )

    issues = list(TextTooLongForUIRule(max_chars=10).check(ctx))

    assert len(issues) == 1
    assert issues[0].rule_code == "TEXT_TOO_LONG_FOR_UI"


def test_text_too_long_for_ui_rule_only_checks_localized_text_with_explicit_limit() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            localized_texts={
                "unbounded": LocalizedText(
                    id="unbounded",
                    text_key="text.long",
                    locale="zh",
                    text="This text is intentionally long",
                ),
                "bounded": LocalizedText(
                    id="bounded",
                    text_key="text.bounded",
                    locale="zh",
                    text="This text is intentionally long",
                    ui_max_len=10,
                ),
            }
        )
    )

    issues = list(TextTooLongForUIRule(max_chars=10).check(ctx))

    assert [issue.target_ref for issue in issues] == ["localized_text:bounded"]


def test_quest_missing_objective_rule_flags_empty_objective() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            quests={
                "quest_empty": Quest(
                    id="quest_empty",
                    title="Empty",
                    localization_keys=["quest.empty.title"],
                )
            }
        )
    )

    issues = list(QuestMissingObjectiveRule().check(ctx))

    assert len(issues) == 1
    assert issues[0].rule_code == "QUEST_MISSING_OBJECTIVE"


def test_placeholder_mismatch_rule_compares_locales_by_text_key() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            dialogues={
                "dlg_en": DialogueRef(
                    id="dlg_en",
                    text_key="dlg.greet",
                    text="Hello {player_name}",
                    locale="en",
                ),
                "dlg_zh": DialogueRef(
                    id="dlg_zh",
                    text_key="dlg.greet",
                    text="你好 {player}",
                    locale="zh",
                ),
            }
        )
    )

    issues = list(PlaceholderMismatchRule().check(ctx))

    assert len(issues) == 1
    assert issues[0].rule_code == "PLACEHOLDER_MISMATCH"
    assert issues[0].evidence[0].data == {
        "expected": ["{player_name}"],
        "actual": ["{player}"],
    }


def test_term_inconsistent_rule_flags_forbidden_terms() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            terms={
                "term_iron_vow": Term(
                    id="term_iron_vow",
                    canonical="Iron Vow",
                    forbidden=["metal promise"],
                )
            },
            quests={
                "quest_term": Quest(
                    id="quest_term",
                    title="Term",
                    objective="Recover the metal promise tablet",
                    localization_keys=["quest.term.objective"],
                )
            },
        )
    )

    issues = list(TermInconsistentRule().check(ctx))

    assert len(issues) == 1
    assert issues[0].rule_code == "TERM_INCONSISTENT"
    assert issues[0].evidence[0].data["canonical"] == "Iron Vow"


def test_term_inconsistent_rule_uses_word_boundaries_for_latin_terms() -> None:
    # A short Latin forbidden term must not flag a coincidental substring (war != warden),
    # but a real standalone use is still caught; CJK (no word boundaries) stays substring-based.
    ctx = AuditContext.from_bundle(
        ContentBundle(
            terms={
                "t_war": Term(id="t_war", canonical="Conflict", forbidden=["war"]),
                "t_lich": Term(id="t_lich", canonical="亡灵君主", forbidden=["巫妖王"]),
            },
            quests={
                "q_fp": Quest(id="q_fp", title="The warden walks toward Northward"),
                "q_real": Quest(id="q_real", title="The great war begins"),
                "q_cjk": Quest(id="q_cjk", title="讨伐巫妖王"),
            },
        )
    )

    flagged = {issue.target_ref for issue in TermInconsistentRule().check(ctx)}

    assert "quest:q_fp" not in flagged  # warden / toward / Northward are not the term "war"
    assert "quest:q_real" in flagged  # a standalone "war" is
    assert "quest:q_cjk" in flagged  # CJK forbidden term still caught


# -- 六要素改造：消息中文化 + 实例化建议动作（policy §15.9） -----------------------------------


def test_missing_localization_key_rule_message_and_action_are_planner_facing() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(quests={"hl_x": Quest(id="hl_x", title="夺回圣钟")})
    )

    issue = list(MissingLocalizationKeyRule().check(ctx))[0]

    assert _HAS_CJK.search(issue.message)
    assert "hl_x" in issue.message
    assert "夺回圣钟" in issue.message
    assert "localization_keys" not in issue.message
    assert "localization_keys" not in issue.suggested_action
    # 建议动作必须与自动修复器实际会写入的值一致（fixers._fix_missing_localization_key）。
    assert "quest.hl_x.objective" in issue.suggested_action


def test_missing_localization_key_rule_locates_to_the_quest_file(tmp_path: Path) -> None:
    """顺带核对：这条问题能被既有的定位器落到文件 + 行号（六要素里的"文件/行号"）。"""
    store = ContentStore(tmp_path)
    store.save(ContentBundle(quests={"hl_x": Quest(id="hl_x", title="夺回圣钟")}))
    saved = store.load()
    issue = list(MissingLocalizationKeyRule().check(AuditContext.from_bundle(saved)))[0]

    annotated = ContentSourceLocator(tmp_path).annotate(issue)

    assert annotated.source_path == "quests/hl_x.json"
    assert annotated.source_line is not None
    text = (tmp_path / annotated.source_path).read_text(encoding="utf-8")
    assert '"localization_keys"' in text.splitlines()[annotated.source_line - 1]


def test_quest_missing_objective_rule_honestly_defers_the_wording_to_a_human() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            quests={
                "hl_y": Quest(id="hl_y", title="护送商队", localization_keys=["quest.hl_y.title"])
            }
        )
    )

    issue = list(QuestMissingObjectiveRule().check(ctx))[0]

    assert _HAS_CJK.search(issue.message)
    assert "hl_y" in issue.message
    assert "objective" not in issue.message
    assert "objective" not in issue.suggested_action
    # 目标文案本身需要策划创作，规则如实说明"需要人判断"，不编一个假的具体目标。
    assert "需要人工判断" in issue.suggested_action
    assert "目标" in issue.suggested_action  # 但仍然指出具体该填哪个字段


def test_text_too_long_for_ui_rule_message_and_action_carry_the_real_numbers() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            dialogues={
                "dlg_long": DialogueRef(id="dlg_long", text_key="dlg.long", text="A" * 20)
            }
        )
    )

    issue = list(TextTooLongForUIRule(max_chars=10).check(ctx))[0]

    assert _HAS_CJK.search(issue.message)
    assert "20" in issue.message and "10" in issue.message
    assert "20" in issue.suggested_action and "10" in issue.suggested_action
    assert "ui_max_len" not in issue.message
    assert "ui_max_len" not in issue.suggested_action


def test_placeholder_mismatch_rule_action_gives_the_exact_placeholder_sets() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            dialogues={
                "dlg_en": DialogueRef(
                    id="dlg_en", text_key="dlg.greet", text="Hello {player_name}", locale="en"
                ),
                "dlg_zh": DialogueRef(
                    id="dlg_zh", text_key="dlg.greet", text="你好 {player}", locale="zh"
                ),
            }
        )
    )

    issue = list(PlaceholderMismatchRule().check(ctx))[0]

    assert _HAS_CJK.search(issue.message)
    assert "dlg.greet" in issue.message
    assert "{player_name}" in issue.suggested_action
    assert "{player}" in issue.suggested_action


def test_term_inconsistent_rule_action_matches_the_fixer() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            terms={
                "term_iron_vow": Term(
                    id="term_iron_vow", canonical="铁誓", forbidden=["metal promise"]
                )
            },
            quests={
                "quest_term": Quest(
                    id="quest_term",
                    title="Term",
                    objective="Recover the metal promise tablet",
                    localization_keys=["quest.term.objective"],
                )
            },
        )
    )

    issue = list(TermInconsistentRule().check(ctx))[0]

    assert _HAS_CJK.search(issue.message)
    assert "target_ref" not in issue.message
    # 建议动作必须与自动修复器实际会做的替换一致（fixers._fix_term_inconsistent）。
    assert "metal promise" in issue.suggested_action
    assert "铁誓" in issue.suggested_action
