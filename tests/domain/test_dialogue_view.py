from __future__ import annotations

from owcopilot.content.models import DialogueChoice, DialogueNode, DialogueTree
from owcopilot.graph.dialogue_view import build_dialogue_flow


def _tree() -> DialogueTree:
    return DialogueTree(
        id="dt_meet",
        title="初遇白盐",
        participants=["npc_white"],
        root_node="n1",
        nodes={
            "n1": DialogueNode(
                id="n1",
                speaker_id="npc_white",
                text="你不是本地人。来矿镇做什么？",
                choices=[
                    DialogueChoice(text="我在查失踪案", next_node="n2"),
                    DialogueChoice(text="只是路过", next_node="n3"),
                ],
            ),
            "n2": DialogueNode(
                id="n2", speaker_id="npc_white", text="那你最好小心宪章会。", next_node="n4"
            ),
            "n3": DialogueNode(id="n3", speaker_id="npc_white", text="路过的人不会问这么多。"),
            "n4": DialogueNode(id="n4", speaker_id="npc_white", text="天黑前别去矿口。"),
        },
    )


def test_node_kinds_classify_root_branch_and_terminals() -> None:
    flow = build_dialogue_flow(_tree(), speaker_names={"npc_white": "白盐"})

    kinds = {n.ref: n.kind for n in flow.nodes}
    assert kinds["n1"] == "root"
    assert kinds["n2"] == "line"  # has an outgoing next_node
    assert kinds["n3"] == "end"  # terminal choice
    assert kinds["n4"] == "end"


def test_choice_and_next_links_become_edges_with_labels() -> None:
    flow = build_dialogue_flow(_tree(), speaker_names={"npc_white": "白盐"})

    by_pair = {(e.source, e.target): e.label for e in flow.edges}
    assert by_pair[("n1", "n2")] == "我在查失踪案"
    assert by_pair[("n1", "n3")] == "只是路过"
    assert by_pair[("n2", "n4")] == ""  # linear next_node carries no choice label


def test_layered_layout_stacks_by_depth_and_resolves_speaker() -> None:
    flow = build_dialogue_flow(_tree(), speaker_names={"npc_white": "白盐"})

    y = {n.ref: n.y for n in flow.nodes}
    assert y["n1"] < y["n2"]
    assert y["n2"] == y["n3"]  # same depth -> same row
    assert y["n2"] < y["n4"]

    root = next(n for n in flow.nodes if n.ref == "n1")
    assert root.focus is True
    assert root.sublabel == "白盐"  # speaker id resolved to name
