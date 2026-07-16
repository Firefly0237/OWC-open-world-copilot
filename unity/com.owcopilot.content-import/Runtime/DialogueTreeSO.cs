// 对白树资产：对应 owcopilot.content.models.DialogueTree（分支会话图，节点由 choices/next 连接）。
// The DialogueTree ScriptableObject. Nodes live INSIDE the tree (like QuestStage inside QuestSO, and
// like the runner's node dict) rather than as separate .asset files, so the node graph is serialized
// with [SerializeReference]: Unity stores each node once and lets a choice/next link point back at the
// SAME node instance, which is what a conversation graph needs — shared and cyclic references (a plain
// nested list would serialize by value and recurse forever on a cycle). Raw string ids stay
// authoritative on every node/choice; the resolved object references are the two-pass importer's
// editor-navigation convenience and are NOT what SoToRunner reads. SoToRunner rebuilds the runner tree
// from the ids (exactly like the off-engine RunnerModelFactory), so the offline-verified DialogueWalker
// is fed the same id-keyed shape no matter how these managed references serialize.

using System;
using System.Collections.Generic;
using UnityEngine;

namespace OWCopilot.ContentImport
{
    /// <summary>One player option out of a node — mirrors owcopilot DialogueChoice.</summary>
    [Serializable]
    public sealed class DialogueTreeChoice
    {
        [TextArea(1, 3)]
        public string Text = "";

        [Tooltip("Target node id (raw, authoritative). Empty/unknown = ends the tree when taken.")]
        public string NextNodeId;

        [Tooltip("Resolved target node (null when NextNodeId is empty/unknown — kept for review).")]
        [SerializeReference]
        public DialogueTreeNode NextNode;

        [Tooltip("Boolean guard expression source; empty = always available.")]
        public string Condition = "";

        // effects reuse the frozen Effect type from the quest-logic layer, so dialogue and quest logic
        // speak ONE effect vocabulary — the DialogueChoice.effects contract on the Python side.
        public List<Effect> Effects = new List<Effect>();
    }

    /// <summary>One conversation node — mirrors owcopilot DialogueNode.</summary>
    [Serializable]
    public sealed class DialogueTreeNode
    {
        public string Id;

        [Tooltip("Speaker entity id (raw, authoritative).")]
        public string SpeakerId;

        [Tooltip("Resolved speaker entity (null when SpeakerId is unknown — kept for review).")]
        public EntitySO Speaker;

        [TextArea(2, 6)]
        public string Text = "";

        public List<DialogueTreeChoice> Choices = new List<DialogueTreeChoice>();

        [Tooltip("Linear-continuation node id when there are no satisfiable choices; empty = ending.")]
        public string NextNodeId;

        [Tooltip("Resolved linear-continuation node (null when empty/unknown — kept for review).")]
        [SerializeReference]
        public DialogueTreeNode NextNode;
    }

    public sealed class DialogueTreeSO : ContentObjectSO
    {
        [Tooltip("Owning quest id (raw, authoritative).")]
        public string QuestId;

        [Tooltip("Resolved owning quest (null when QuestId is unknown — kept for review).")]
        public QuestSO Quest;

        [Tooltip("Entry node id — the walk starts here.")]
        public string RootNodeId;

        // The node graph, serialized by managed reference so choice/next links can share instances and
        // form cycles. See the file header for why this is [SerializeReference] and not a plain list.
        [SerializeReference]
        public List<DialogueTreeNode> Nodes = new List<DialogueTreeNode>();
    }
}
