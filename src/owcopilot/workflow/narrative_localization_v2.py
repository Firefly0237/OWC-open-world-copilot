"""Stable source-string identities, separate from generated prose and translations.

The host derives keys from content identities, as Yarn line IDs do. This is an
OWC source table/export contract; it does not execute Yarn or claim translation.
"""

from __future__ import annotations

from ..content.hash import content_hash
from ..content.lang import detect_language
from ..content.models import DialogueTree, LocalizedText, Origin, Quest, ReviewStatus

DRAFT_COMPILER_VERSION = "quest-draft-v2.2"
DIALOGUE_COMPILER_VERSION = "dialogue-ir-v2.2"


def quest_source_strings(quest: Quest) -> dict[str, str]:
    prefix = "quest." + quest.id
    values = {prefix + ".title": quest.title, prefix + ".objective": quest.objective}
    values.update({prefix + ".stage." + row.id + ".summary": row.summary for row in quest.stages})
    if quest.logic:
        values.update(
            {
                prefix + ".branch." + row.id + ".outcome": row.outcome
                for row in quest.logic.branches
                if row.outcome
            }
        )
    return {key: text for key, text in values.items() if text}


def with_quest_localization(quest: Quest) -> Quest:
    """Derive real source entries; do not add dummy keys merely to satisfy audit."""
    values = quest_source_strings(quest)
    metadata = dict(quest.metadata)
    metadata["localization_v2"] = {
        "compiler_version": DRAFT_COMPILER_VERSION,
        "source_strings": values,
        "source_strings_hash": content_hash(values),
        "translation_status": "source_only_not_translated",
    }
    return quest.model_copy(
        update={"localization_keys": list(values), "metadata": metadata}, deep=True
    )


def localize_tree(tree: DialogueTree) -> DialogueTree:
    result = tree.model_copy(deep=True)
    for node in result.nodes.values():
        if node.text and not node.text_key:
            node.text_key = f"dlg.{tree.id}.{node.id}"
        for index, choice in enumerate(node.choices):
            if choice.text and not choice.text_key:
                choice.text_key = f"dlg.{tree.id}.{node.id}.choice.{index}"
    return result


def source_localization_entries(
    quest: Quest, tree: DialogueTree | None = None
) -> list[LocalizedText]:
    values = quest_source_strings(quest)
    declaration = quest.metadata.get("localization_v2")
    if declaration is None:
        return []  # Historical/imported keys have no new compiler attestation.
    if (
        declaration.get("source_strings") != values
        or declaration.get("source_strings_hash") != content_hash(values)
        or quest.localization_keys != list(values)
    ):
        raise ValueError("本地化键／原文表与实际任务正文不一致")
    if tree:
        for node in tree.nodes.values():
            if node.text and node.text_key:
                if node.text_key in values:
                    raise ValueError("对白本地化键重复")
                values[node.text_key] = node.text
            for choice in node.choices:
                if choice.text and choice.text_key:
                    if choice.text_key in values:
                        raise ValueError("选项本地化键重复")
                    values[choice.text_key] = choice.text
        presentation = tree.metadata.get("player_presentation_v1", {})
        for branch_id, text in presentation.get("endings", {}).items():
            key = f"dlg.{tree.id}.ending.{branch_id}"
            if key in values:
                raise ValueError("结局本地化键重复")
            values[key] = text
    return [
        LocalizedText(
            id="loc_" + content_hash(key)[:24],
            text_key=key,
            locale=detect_language(text).dominant,
            text=text,
            origin=Origin.AI_DRAFT,
            review_status=ReviewStatus.PENDING_REVIEW,
            metadata={"source_quest_id": quest.id, "source_only": True},
        )
        for key, text in values.items()
    ]
