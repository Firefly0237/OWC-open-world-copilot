// 把导入得到的 QuestSO（含原生 QuestLogic）投影成 Core 运行器模型，喂给同一个 QuestRunner。
// Projects an imported QuestSO (with its native QuestLogic) into the engine-free Core runner models,
// so the in-editor playtest window drives the SAME QuestRunner the off-engine cross-language test
// exercises. The Core runner is the offline-verified part; this SO->runner bridge and the window
// that calls it are Editor-only and require real Unity (see the window header).

using System.Collections.Generic;
using OWCopilot.ContentImport.Core;

namespace OWCopilot.ContentImport.Editor
{
    public static class SoToRunner
    {
        public static RunnerQuest FromQuest(QuestSO quest)
        {
            var runner = new RunnerQuest { Id = quest.Id };
            if (quest.Stages != null)
            {
                foreach (QuestStage stage in quest.Stages)
                {
                    runner.StageIds.Add(stage.Id);
                }
            }
            runner.Logic = quest.Logic != null ? FromLogic(quest.Logic) : null;
            return runner;
        }

        // Rebuild the runner tree from the SO's RAW ids (not the [SerializeReference] object links), so
        // DialogueWalker is fed the SAME id-keyed shape RunnerModelFactory.TreeFromDto produces from the
        // JSON DTO — the offline-verified walker path is identical whichever way the tree was loaded.
        // The one subtlety: Unity serializes a null string as "", so a "no next node" (an ending) reads
        // back as "" here; we restore it to null, because DialogueWalker treats a null next as the
        // terminal and a non-null "" as a (dangling) node id.
        public static RunnerDialogueTree FromTree(DialogueTreeSO tree)
        {
            var runner = new RunnerDialogueTree { Id = tree.Id, RootNode = tree.RootNodeId ?? string.Empty };
            if (tree.Nodes == null)
            {
                return runner;
            }
            foreach (DialogueTreeNode node in tree.Nodes)
            {
                if (node == null || string.IsNullOrEmpty(node.Id))
                {
                    continue;
                }
                var runnerNode = new RunnerDialogueNode { Id = node.Id, NextNode = NullIfEmpty(node.NextNodeId) };
                if (node.Choices != null)
                {
                    foreach (DialogueTreeChoice choice in node.Choices)
                    {
                        runnerNode.Choices.Add(new RunnerDialogueChoice
                        {
                            Condition = choice.Condition ?? string.Empty,
                            NextNode = NullIfEmpty(choice.NextNodeId),
                            Effects = FromEffects(choice.Effects),
                        });
                    }
                }
                runner.Nodes[node.Id] = runnerNode;
            }
            return runner;
        }

        private static string NullIfEmpty(string value) => string.IsNullOrEmpty(value) ? null : value;

        private static RunnerQuestLogic FromLogic(QuestLogic logic)
        {
            var result = new RunnerQuestLogic { Precondition = logic.Precondition ?? string.Empty };
            foreach (LogicVar v in logic.Variables)
            {
                result.Variables.Add(new RunnerLogicVar { Id = v.Id, Default = FromValue(v.Default) });
            }
            foreach (StageLogic sl in logic.StageLogic)
            {
                result.StageLogic.Add(new RunnerStageLogic
                {
                    StageId = sl.StageId,
                    Precondition = sl.Precondition ?? string.Empty,
                    EffectsOnComplete = FromEffects(sl.EffectsOnComplete),
                });
            }
            foreach (Branch b in logic.Branches)
            {
                result.Branches.Add(new RunnerBranch
                {
                    Id = b.Id,
                    FromStage = b.FromStage,
                    Condition = b.Condition ?? string.Empty,
                    ToStage = b.ToStage ?? string.Empty,
                    Outcome = b.Outcome ?? string.Empty,
                    Effects = FromEffects(b.Effects),
                });
            }
            return result;
        }

        private static List<RunnerEffect> FromEffects(List<Effect> effects)
        {
            var result = new List<RunnerEffect>();
            if (effects == null)
            {
                return result;
            }
            foreach (Effect e in effects)
            {
                result.Add(new RunnerEffect { Var = e.Var, Op = OpString(e.Op), Value = FromValue(e.Value) });
            }
            return result;
        }

        private static RunnerValue FromValue(LogicValue value)
        {
            switch (value.Kind)
            {
                case LogicValueKind.Bool:
                    return RunnerValue.OfBool(value.BoolValue);
                case LogicValueKind.Int:
                    return RunnerValue.OfInt(value.IntValue);
                default:
                    return RunnerValue.OfString(value.StringValue);
            }
        }

        // The Core runner uses the raw wire op string ("set"/"inc"/"dec"), the same tokens Python
        // emits in the effect string and applies in WorldState.apply.
        private static string OpString(EffectOp op)
        {
            switch (op)
            {
                case EffectOp.Inc:
                    return "inc";
                case EffectOp.Dec:
                    return "dec";
                default:
                    return "set";
            }
        }
    }
}
