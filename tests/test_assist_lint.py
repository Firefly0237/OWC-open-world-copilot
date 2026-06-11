from __future__ import annotations

from owcopilot.assist.lint import lint_text
from owcopilot.content.models import ContentBundle, Entity, EntityType, Term


def test_lint_text_flags_length_and_forbidden_terms() -> None:
    bundle = ContentBundle(
        terms={
            "term_iron_vow": Term(
                id="term_iron_vow",
                canonical="Iron Vow",
                forbidden=["metal promise"],
            )
        }
    )

    issues = lint_text("This metal promise is too long", bundle=bundle, max_chars=5)

    assert {issue.code for issue in issues} == {"TEXT_TOO_LONG_FOR_UI", "TERM_INCONSISTENT"}


def test_lint_text_flags_entity_mentions_outside_allowed_context() -> None:
    bundle = ContentBundle(
        entities={
            "npc_aldric": Entity(id="npc_aldric", name="Aldric", type=EntityType.NPC),
            "npc_mara": Entity(id="npc_mara", name="Mara", type=EntityType.NPC),
        }
    )

    issues = lint_text(
        "Mara is here",
        bundle=bundle,
        max_chars=40,
        allowed_entity_ids={"npc_aldric"},
    )

    assert [issue.code for issue in issues] == ["FORBIDDEN_ENTITY_REF"]
    assert issues[0].target == "npc_mara"
