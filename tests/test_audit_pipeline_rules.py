from __future__ import annotations

from owcopilot.audit.context import AuditContext
from owcopilot.audit.rules.pipeline_rules import (
    MissingLocalizationKeyRule,
    PlaceholderMismatchRule,
    QuestMissingObjectiveRule,
    TermInconsistentRule,
    TextTooLongForUIRule,
)
from owcopilot.content.models import ContentBundle, DialogueRef, LocalizedText, Quest, Term


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
