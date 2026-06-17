"""B10 · inline-edit the worldview style guide (body + rules) — direct human-edit, lands at once."""

from __future__ import annotations

import pytest

from owcopilot.app.actions import update_style_guide_action
from owcopilot.content.models import ContentBundle, StyleGuide
from owcopilot.content.store import ContentStore


def _seed(root) -> None:
    ContentStore(root).save(
        ContentBundle(
            style_guides={
                "style_guide": StyleGuide(id="style_guide", body="旧世界观。", rules=["旧守则一"])
            }
        )
    )


def test_edit_body_and_rules_round_trips(tmp_path) -> None:
    root = tmp_path / "content"
    _seed(root)
    res = update_style_guide_action(root, body="新的世界观正文。", rules=["守则一", "守则二", "  "])
    assert set(res["changed"]) == {"body", "rules"}
    reloaded = ContentStore(root).load().style_guides["style_guide"]
    assert reloaded.body == "新的世界观正文。"
    assert reloaded.rules == ["守则一", "守则二"]  # blank rule dropped, full fidelity persisted


def test_edit_body_only_keeps_rules(tmp_path) -> None:
    root = tmp_path / "content"
    _seed(root)
    update_style_guide_action(root, body="只改正文。")
    reloaded = ContentStore(root).load().style_guides["style_guide"]
    assert reloaded.body == "只改正文。" and reloaded.rules == ["旧守则一"]


def test_unknown_guide_is_clean_error(tmp_path) -> None:
    root = tmp_path / "content"
    _seed(root)
    with pytest.raises(ValueError, match="风格指南不存在"):
        update_style_guide_action(root, guide_id="ghost", body="x")
