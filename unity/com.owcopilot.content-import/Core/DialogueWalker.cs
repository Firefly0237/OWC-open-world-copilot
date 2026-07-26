// 对白树的确定性行走（dialogue.py walk_dialogue_tree 的逐行移植，UnityEngine-free）。与任务模拟共用
// 同一套状态/条件/效果原语，转移语义不会与 QuestRunner 分叉。
// A line-for-line port of the FROZEN dialogue.py::walk_dialogue_tree. It reuses the SAME primitives
// as QuestRunner (WorldState / QuestLogicEval.Truthy / the effect string) so dialogue and quest
// transition semantics cannot diverge. Statuses: completed | dangling | deadlock | cycle.
//   * a queued selector is "<node_id>:<choice_index>"; without a matching queued selector the walk
//     takes the FIRST condition-satisfiable choice (mirroring branch selection);
//   * deadlock = a node whose choices are all unsatisfiable AND has no next_node fallback;
//   * dangling = a link points at a node that does not exist;
//   * seeding mirrors _seed: only choice-condition refs setdefault=false, then initial_state (there
//     is no variable list on a tree). Feed the resulting final_state into QuestRunner.Simulate as
//     initialState to close the "a dialogue choice changes the world" loop.

using System.Collections.Generic;

namespace OWCopilot.ContentImport.Core
{
    public static class DialogueWalker
    {
        public const int DefaultMaxSteps = 200;

        public static DialogueWalk Walk(
            RunnerDialogueTree tree,
            IReadOnlyList<string> choices = null,
            IReadOnlyDictionary<string, RunnerValue> initialState = null,
            int maxSteps = DefaultMaxSteps)
        {
            var queued = choices != null ? new List<string>(choices) : new List<string>();
            WorldState state = Seed(tree, initialState);
            var walk = new DialogueWalk { TreeId = tree.Id, Status = "completed" };
            var visited = new HashSet<string>();
            string pos = string.IsNullOrEmpty(tree.RootNode) ? null : tree.RootNode;

            for (int step = 0; step < maxSteps; step++)
            {
                if (pos == null)
                {
                    break;
                }
                if (!tree.Nodes.ContainsKey(pos))
                {
                    walk.Status = "dangling";
                    walk.Message = "link points at nonexistent node '" + pos + "'";
                    break;
                }
                if (visited.Contains(pos))
                {
                    walk.Status = "cycle";
                    walk.Message = "cycle detected at node '" + pos + "'";
                    break;
                }
                visited.Add(pos);
                RunnerDialogueNode node = tree.Nodes[pos];
                var walkStep = new DialogueWalkStep { NodeId = pos };
                walk.Path.Add(pos);

                if (node.Choices != null && node.Choices.Count > 0)
                {
                    int chosenIndex = -1;
                    for (int i = 0; i < node.Choices.Count; i++)
                    {
                        string selector = pos + ":" + i;
                        if (queued.Contains(selector)
                            && QuestLogicEval.Truthy(state.Values, node.Choices[i].Condition))
                        {
                            chosenIndex = i;
                            queued.Remove(selector);
                            break;
                        }
                    }
                    if (chosenIndex < 0)
                    {
                        for (int i = 0; i < node.Choices.Count; i++)
                        {
                            if (QuestLogicEval.Truthy(state.Values, node.Choices[i].Condition))
                            {
                                chosenIndex = i;
                                break;
                            }
                        }
                    }
                    if (chosenIndex < 0)
                    {
                        if (node.NextNode == null)
                        {
                            walk.Steps.Add(walkStep);
                            walk.Status = "deadlock";
                            walk.Message = "node '" + pos + "' has no satisfiable choice";
                            break;
                        }
                        pos = node.NextNode; // linear fallback when every choice is gated off
                    }
                    else
                    {
                        RunnerDialogueChoice choice = node.Choices[chosenIndex];
                        walkStep.Chosen = pos + ":" + chosenIndex;
                        foreach (RunnerEffect effect in choice.Effects)
                        {
                            state.Apply(effect.Var, effect.Op, effect.Value);
                            walkStep.Effects.Add(
                                effect.Var + " " + effect.Op + " " + effect.Value.ToWireString());
                        }
                        pos = choice.NextNode;
                    }
                }
                else
                {
                    pos = node.NextNode;
                }
                walk.Steps.Add(walkStep);
            }

            foreach (KeyValuePair<string, RunnerValue> kv in state.Values)
            {
                walk.FinalState[kv.Key] = kv.Value;
            }
            return walk;
        }

        // Mirror _seed: condition refs default to false, then override with initial_state.
        private static WorldState Seed(
            RunnerDialogueTree tree, IReadOnlyDictionary<string, RunnerValue> initial)
        {
            var state = new WorldState();
            foreach (RunnerDialogueNode node in tree.Nodes.Values)
            {
                if (node.Choices == null)
                {
                    continue;
                }
                foreach (RunnerDialogueChoice choice in node.Choices)
                {
                    if (string.IsNullOrWhiteSpace(choice.Condition))
                    {
                        continue;
                    }
                    try
                    {
                        foreach (string reference in
                                 QuestLogicExpr.RefsIn(QuestLogicExpr.Parse(choice.Condition)))
                        {
                            state.SetDefault(reference, RunnerValue.OfBool(false));
                        }
                    }
                    catch (LogicSyntaxException)
                    {
                        // a broken condition is unsatisfiable; the audit already flags it
                    }
                }
            }
            if (initial != null)
            {
                foreach (KeyValuePair<string, RunnerValue> kv in initial)
                {
                    state.Values[kv.Key] = kv.Value;
                }
            }
            return state;
        }
    }
}
