from __future__ import annotations

from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest
from owcopilot.content.store import ContentStore
from owcopilot.pipeline.project import ProjectContext


def test_project_context_open_loads_content_and_builds_indexes(tmp_path) -> None:
    content_root = tmp_path / "content"
    ContentStore(content_root).save(
        ContentBundle(
            entities={
                "npc_aldric": Entity(
                    id="npc_aldric",
                    name="Aldric",
                    type=EntityType.NPC,
                    description="Caravan master",
                )
            },
            quests={"q1": Quest(id="q1", title="Caravan Quest", giver_npc="npc_aldric")},
        )
    )

    project = ProjectContext.open(content_root)
    try:
        assert project.bundle.entities["npc_aldric"].name == "Aldric"
        assert project.graph.has_node("quest:q1")
        assert project.context_builder.build("Aldric").refs
    finally:
        project.close()


def test_project_context_reload_rebuilds_indexes_after_content_changes(tmp_path) -> None:
    content_root = tmp_path / "content"
    store = ContentStore(content_root)
    store.save(ContentBundle())
    project = ProjectContext.open(content_root)
    try:
        store.save(
            ContentBundle(
                entities={"npc_mara": Entity(id="npc_mara", name="Mara", type=EntityType.NPC)}
            )
        )

        project.reload()

        assert "npc_mara" in project.bundle.entities
        assert project.context_builder.build("Mara").refs == ["entity:npc_mara"]
    finally:
        project.close()
