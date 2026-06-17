from __future__ import annotations

from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest
from owcopilot.graph.timeline_view import build_timeline_view


def _world() -> ContentBundle:
    return ContentBundle(
        entities={
            "ev_war": Entity(
                id="ev_war", name="旧战爆发", type=EntityType.EVENT, metadata={"timeline_order": 1}
            ),
            "ev_drought": Entity(
                id="ev_drought",
                name="矿脉断流",
                type=EntityType.EVENT,
                metadata={"timeline_order": 5},
            ),
            "ev_floating": Entity(id="ev_floating", name="无名传说", type=EntityType.EVENT),
        },
        quests={
            "q_a": Quest(id="q_a", title="调查枯树", timeline_order=2),
            "q_b": Quest(id="q_b", title="结盟密谈", timeline_order=8, prerequisites=["q_a"]),
            # requires a prereq that occurs LATER -> the audit flags it
            "q_bad": Quest(
                id="q_bad", title="揭穿真相", timeline_order=3, prerequisites=["q_late"]
            ),
            "q_late": Quest(id="q_late", title="终局", timeline_order=10),
            "q_orphan": Quest(id="q_orphan", title="未定序支线"),
        },
    )


def test_dense_rank_collapses_sparse_orders() -> None:
    view = build_timeline_view(_world())

    # six distinct order values (1,2,3,5,8,10) -> dense ranks 0..5, no gaps
    assert view.rank_count == 6
    rank_by_ref = {item.ref: item.rank for item in view.items}
    assert rank_by_ref["entity:ev_war"] == 0
    assert rank_by_ref["quest:q_a"] == 1
    assert rank_by_ref["quest:q_bad"] == 2
    assert rank_by_ref["entity:ev_drought"] == 3
    assert rank_by_ref["quest:q_b"] == 4
    assert rank_by_ref["quest:q_late"] == 5


def test_events_and_quests_share_the_axis_with_stable_kinds() -> None:
    view = build_timeline_view(_world())

    kinds = {item.ref: item.kind for item in view.items}
    assert kinds["entity:ev_war"] == "event"
    assert kinds["quest:q_a"] == "quest"


def test_audit_violation_is_hung_on_the_offending_item_and_edge() -> None:
    view = build_timeline_view(_world())

    bad = next(item for item in view.items if item.ref == "quest:q_bad")
    assert "前置任务没有排在它之前" in bad.flags

    by_target = {(dep.source, dep.target): dep.violation for dep in view.dependencies}
    assert by_target[("quest:q_late", "quest:q_bad")] is True
    # a healthy prereq (q_a before q_b) is not reddened
    assert by_target[("quest:q_a", "quest:q_b")] is False


def test_items_without_order_fall_into_unsequenced_bucket() -> None:
    view = build_timeline_view(_world())

    unseq = {(entry.ref, entry.kind) for entry in view.unsequenced}
    assert ("quest:q_orphan", "quest") in unseq
    assert ("entity:ev_floating", "event") in unseq
    # unsequenced items never leak into the ordered axis
    ordered_refs = {item.ref for item in view.items}
    assert "quest:q_orphan" not in ordered_refs
