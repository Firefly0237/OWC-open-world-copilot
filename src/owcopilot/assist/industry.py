# 游戏内容 prompt 用的行业调研 rubric 片段。
"""Concrete content checks shared by drafting and critique prompts.

The existing helper and constant entry points preserve caller compatibility.
Each identifier selects actual checks rather than an evidence or quality claim.
"""

from __future__ import annotations

SOURCE_NOTES: dict[str, str] = {
    "QUEST_ACTIONS": (
        "quest structure: quests are action sequences toward a goal/reward, "
        "generated from world facts such as characters, locations and items, plus motivations"
    ),
    "DIALOGUE_GROUNDING": (
        "side-quest dialogue: dialogue is branching, lore/persona/"
        "backstory/relationship faithful, and reveals quest details to the player"
    ),
    "STATEFUL_SCRIPT": (
        "production dialogue scripting: speaker-attributed lines, player options, node jumps, "
        "variables and conditional options"
    ),
    "DEPENDENCY_FLOW": (
        "dependency-aware RPG prompt pipelines: structured JSON and explicit data flow reduce "
        "drift/hallucination; review criteria include completeness, consistency, coherence, "
        "diversity and actionability"
    ),
    "CHARACTER_CONSTRAINTS": (
        "designer-guided narrative: high-level designer criteria, NPC personality "
        "bias and validation against the unfolding narrative"
    ),
    "AUTHOR_GOALS": (
        "game plot co-authoring: authorial intent/high-level plot outlines are transformed into "
        "concrete character actions grounded in game world state"
    ),
}

QUEST_RUBRIC_SOURCES = ("QUEST_ACTIONS", "DEPENDENCY_FLOW", "AUTHOR_GOALS")
CHARACTER_RUBRIC_SOURCES = ("DIALOGUE_GROUNDING", "CHARACTER_CONSTRAINTS", "AUTHOR_GOALS")
DIALOGUE_RUBRIC_SOURCES = ("DIALOGUE_GROUNDING", "STATEFUL_SCRIPT")
BARK_RUBRIC_SOURCES = ("DIALOGUE_GROUNDING", "CHARACTER_CONSTRAINTS")
FLAVOR_RUBRIC_SOURCES = ("DEPENDENCY_FLOW", "CHARACTER_CONSTRAINTS")
LOGIC_RUBRIC_SOURCES = ("STATEFUL_SCRIPT", "QUEST_ACTIONS")
WORLD_RUBRIC_SOURCES = (
    "DEPENDENCY_FLOW",
    "QUEST_ACTIONS",
    "DIALOGUE_GROUNDING",
    "AUTHOR_GOALS",
)


def industry_source_block(*source_ids: str) -> str:
    """Return the concrete checks embedded into drafting and critique prompts."""
    lines = ["CONTENT CHECKS - use these dimensions for this rubric:"]
    for source_id in source_ids:
        try:
            note = SOURCE_NOTES[source_id]
        except KeyError as exc:  # pragma: no cover - programmer error, caught by tests
            raise ValueError(f"unknown industry source id: {source_id}") from exc
        lines.append(f"- [{source_id}] {note}.")
    lines.append(
        "Do not add extra critique dimensions beyond these checks."
    )
    return "\n".join(lines)
