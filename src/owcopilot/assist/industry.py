"""Industry-researched rubric snippets for game-content prompts.

These notes are deliberately short enough to live inside system prompts. The fuller rationale and
URLs are documented in ``project_docs/行业调研提示词依据.md``; keep the source ids stable so tests
can assert that critique/quality prompts do not drift back to unsourced taste.
"""

from __future__ import annotations

SOURCE_NOTES: dict[str, str] = {
    "CONAN_QUESTS": (
        "commercial-quest structural analysis: quests are action sequences toward a goal/reward, "
        "generated from world facts such as characters, locations and items, plus motivations"
    ),
    "KNUDGE_OUTER_WORLDS": (
        "Obsidian The Outer Worlds side-quest dialogue data: dialogue is branching, lore/persona/"
        "backstory/relationship faithful, and reveals quest details to the player"
    ),
    "YARN_SPINNER": (
        "production dialogue scripting: speaker-attributed lines, player options, node jumps, "
        "variables and conditional options"
    ),
    "RPG_PIPELINE_2026": (
        "dependency-aware RPG prompt pipelines: structured JSON and explicit data flow reduce "
        "drift/hallucination; review criteria include completeness, consistency, coherence, "
        "diversity and actionability"
    ),
    "PANGEA_RPG": (
        "designer-guided RPG narrative generation: high-level designer criteria, NPC personality "
        "bias and validation against the unfolding narrative"
    ),
    "STORYVERSE_AUTHORIAL": (
        "game plot co-authoring: authorial intent/high-level plot outlines are transformed into "
        "concrete character actions grounded in game world state"
    ),
}

SOURCE_URLS: dict[str, str] = {
    "CONAN_QUESTS": "https://arxiv.org/abs/1808.06217",
    "KNUDGE_OUTER_WORLDS": "https://arxiv.org/abs/2212.10618",
    "YARN_SPINNER": "https://docs.yarnspinner.dev/2.5/beginners-guide/syntax-basics",
    "RPG_PIPELINE_2026": "https://arxiv.org/abs/2604.25482",
    "PANGEA_RPG": "https://arxiv.org/abs/2404.19721",
    "STORYVERSE_AUTHORIAL": "https://arxiv.org/abs/2405.13042",
}

QUEST_RUBRIC_SOURCES = ("CONAN_QUESTS", "RPG_PIPELINE_2026", "STORYVERSE_AUTHORIAL")
CHARACTER_RUBRIC_SOURCES = ("KNUDGE_OUTER_WORLDS", "PANGEA_RPG", "STORYVERSE_AUTHORIAL")
DIALOGUE_RUBRIC_SOURCES = ("KNUDGE_OUTER_WORLDS", "YARN_SPINNER")
BARK_RUBRIC_SOURCES = ("KNUDGE_OUTER_WORLDS", "PANGEA_RPG")
FLAVOR_RUBRIC_SOURCES = ("RPG_PIPELINE_2026", "PANGEA_RPG")
LOGIC_RUBRIC_SOURCES = ("YARN_SPINNER", "CONAN_QUESTS")
WORLD_RUBRIC_SOURCES = (
    "RPG_PIPELINE_2026",
    "CONAN_QUESTS",
    "KNUDGE_OUTER_WORLDS",
    "STORYVERSE_AUTHORIAL",
)


def industry_source_block(*source_ids: str) -> str:
    """Return the compact source map embedded into subjective prompt rubrics."""
    lines = ["INDUSTRY SOURCE MAP - use only these researched bases for this rubric:"]
    for source_id in source_ids:
        try:
            note = SOURCE_NOTES[source_id]
        except KeyError as exc:  # pragma: no cover - programmer error, caught by tests
            raise ValueError(f"unknown industry source id: {source_id}") from exc
        lines.append(f"- [{source_id}] {note}.")
    lines.append(
        "Do not add extra critique dimensions unless they are tied to one of these sources."
    )
    return "\n".join(lines)
