"""Headless render test for the Workbench dashboard via streamlit.testing.AppTest.

This executes the real dashboard script in-process against a real project directory, so import
errors, widget-key collisions and action-layer wiring regressions fail here instead of in a
customer's browser. Skipped automatically when the optional `app` extra is not installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from owcopilot.content.models import (  # noqa: E402
    ContentBundle,
    Entity,
    EntityType,
    Quest,
    Relation,
)
from owcopilot.content.store import ContentStore  # noqa: E402

_DASHBOARD = str(Path(__file__).resolve().parents[1] / "src" / "owcopilot" / "app" / "dashboard.py")


def _seed_project(tmp_path: Path) -> str:
    root = tmp_path / "content"
    ContentStore(root).save(
        ContentBundle(
            entities={
                "npc_mara": Entity(
                    id="npc_mara", name="玛拉", type=EntityType.NPC, description="边境斥候。"
                ),
                "loc_fort": Entity(
                    id="loc_fort",
                    name="边境要塞",
                    type=EntityType.LOCATION,
                    description="北境要塞。",
                ),
            },
            relations=[Relation(source="npc_mara", target="loc_fort", kind="located_in")],
            quests={
                "quest_patrol": Quest(
                    id="quest_patrol",
                    title="巡逻边境",
                    giver_npc="npc_mara",
                    location="loc_fort",
                    objective="天黑前巡视边境线。",
                    localization_keys=["quest.quest_patrol.objective"],
                )
            },
        )
    )
    return str(root)


def test_dashboard_renders_without_project() -> None:
    at = AppTest.from_file(_DASHBOARD, default_timeout=30)
    at.run()
    assert not at.exception, at.exception


def test_dashboard_renders_full_workbench_with_project(tmp_path: Path) -> None:
    root = _seed_project(tmp_path)
    at = AppTest.from_file(_DASHBOARD, default_timeout=60)
    at.session_state["content_root"] = root
    at.run()
    assert not at.exception, at.exception
    # The overview tab rendered real metrics from the seeded world.
    metric_labels = {metric.label for metric in at.metric}
    assert "实体" in metric_labels and "任务" in metric_labels
