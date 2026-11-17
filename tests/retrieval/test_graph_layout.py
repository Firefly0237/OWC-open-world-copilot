from __future__ import annotations

from owcopilot.graph.layout import concentric_layout, layered_layout
from owcopilot.graph.ordering import timeline_order_of


def test_concentric_layout_places_focus_at_centre_and_is_deterministic() -> None:
    distances = {"focus": 0, "b": 1, "a": 1, "d": 1, "c": 1}
    pos = concentric_layout(distances, center=(300.0, 200.0), ring_gap=100.0)

    assert pos["focus"] == (300.0, 200.0)
    # ring-1 nodes sit at radius ring_gap from the centre (within float tolerance)
    for node in ("a", "b", "c", "d"):
        dx = pos[node][0] - 300.0
        dy = pos[node][1] - 200.0
        assert round((dx * dx + dy * dy) ** 0.5) == 100
    assert concentric_layout(distances, center=(300.0, 200.0), ring_gap=100.0) == pos


def test_layered_layout_rows_by_depth_and_keeps_orphans_visible() -> None:
    adjacency = {"root": ["a", "b"], "a": ["c"], "b": [], "c": []}
    pos = layered_layout(["root", "a", "b", "c", "orphan"], adjacency, root="root")

    assert pos["root"][1] < pos["a"][1]  # deeper rows are lower
    assert pos["a"][1] == pos["b"][1]  # same depth -> same row
    assert pos["a"][1] < pos["c"][1]
    assert "orphan" in pos  # unreachable nodes still get a position


def test_timeline_order_of_reads_metadata_then_tag() -> None:
    assert timeline_order_of({"timeline_order": 5}, []) == 5
    assert timeline_order_of({"timeline_order": "7"}, []) == 7
    assert timeline_order_of(None, ["order=3"]) == 3
    assert timeline_order_of({}, ["unrelated"]) is None
    assert timeline_order_of({"timeline_order": True}, []) is None  # bool is not an order
