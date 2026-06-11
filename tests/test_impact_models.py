from __future__ import annotations

from owcopilot.impact.models import Change, ChangeSet, ChangeType, ImpactItem, ImpactLevel


def test_impact_models_group_items_by_level() -> None:
    item = ImpactItem(
        target_ref="quest:q1",
        level=ImpactLevel.MUST_CHANGE,
        distance=1,
        reason="direct",
        source_change="entity:npc_a",
    )

    assert ChangeSet(
        changes=[Change(change_type=ChangeType.ENTITY_RENAME, target_ref="entity:npc_a")]
    ).changes[0].change_type is ChangeType.ENTITY_RENAME
    assert item.level is ImpactLevel.MUST_CHANGE
