// 把解析后的 content_bundle DTO 投影成运行器领域模型（UnityEngine-free）。冻结联合值
// LogicVar.default / Effect.value 被 Newtonsoft 反序列化为 bool/long/string，这里窄化成 RunnerValue。
// Projects the parsed content-bundle DTOs (Core/Dtos.cs) into the runner domain models the state
// machine consumes. This is the off-engine (JSON) build path used by the cross-language test; the
// in-editor SO build path is Editor/SoToRunner.cs. Both feed the SAME QuestRunner/DialogueWalker, so
// correctness is exercised in one place.

using System.Collections.Generic;

namespace OWCopilot.ContentImport.Core
{
    public static class RunnerModelFactory
    {
        public static RunnerQuest QuestFromDto(QuestDto dto)
        {
            var quest = new RunnerQuest { Id = dto.Id };
            if (dto.Stages != null)
            {
                foreach (QuestStageDto stage in dto.Stages)
                {
                    quest.StageIds.Add(stage.Id);
                }
            }
            quest.Logic = dto.Logic != null ? LogicFromDto(dto.Logic) : null;
            return quest;
        }

        public static RunnerDialogueTree TreeFromDto(DialogueTreeDto dto)
        {
            var tree = new RunnerDialogueTree
            {
                Id = dto.Id,
                RootNode = dto.RootNode ?? string.Empty,
            };
            if (dto.Nodes != null)
            {
                foreach (KeyValuePair<string, DialogueNodeDto> kv in dto.Nodes)
                {
                    DialogueNodeDto nodeDto = kv.Value;
                    var node = new RunnerDialogueNode
                    {
                        Id = string.IsNullOrEmpty(nodeDto.Id) ? kv.Key : nodeDto.Id,
                        NextNode = nodeDto.NextNode,
                    };
                    if (nodeDto.Choices != null)
                    {
                        foreach (DialogueChoiceDto choice in nodeDto.Choices)
                        {
                            node.Choices.Add(new RunnerDialogueChoice
                            {
                                Condition = choice.Condition ?? string.Empty,
                                NextNode = choice.NextNode,
                                Effects = Effects(choice.Effects),
                            });
                        }
                    }
                    tree.Nodes[kv.Key] = node;
                }
            }
            return tree;
        }

        private static RunnerQuestLogic LogicFromDto(QuestLogicDto dto)
        {
            var logic = new RunnerQuestLogic { Precondition = dto.Precondition ?? string.Empty };
            if (dto.Variables != null)
            {
                foreach (LogicVarDto v in dto.Variables)
                {
                    logic.Variables.Add(new RunnerLogicVar
                    {
                        Id = v.Id,
                        Default = ValueFromWire(v.Default, RunnerValue.OfBool(false)),
                    });
                }
            }
            if (dto.StageLogic != null)
            {
                foreach (StageLogicDto s in dto.StageLogic)
                {
                    logic.StageLogic.Add(new RunnerStageLogic
                    {
                        StageId = s.StageId,
                        Precondition = s.Precondition ?? string.Empty,
                        EffectsOnComplete = Effects(s.EffectsOnComplete),
                    });
                }
            }
            if (dto.Branches != null)
            {
                foreach (BranchDto b in dto.Branches)
                {
                    logic.Branches.Add(new RunnerBranch
                    {
                        Id = b.Id,
                        FromStage = b.FromStage,
                        Condition = b.Condition ?? string.Empty,
                        ToStage = b.ToStage ?? string.Empty,
                        Outcome = b.Outcome ?? string.Empty,
                        Effects = Effects(b.Effects),
                    });
                }
            }
            return logic;
        }

        private static List<RunnerEffect> Effects(List<EffectDto> dtos)
        {
            var result = new List<RunnerEffect>();
            if (dtos == null)
            {
                return result;
            }
            foreach (EffectDto e in dtos)
            {
                result.Add(new RunnerEffect
                {
                    Var = e.Var,
                    Op = string.IsNullOrEmpty(e.Op) ? "set" : e.Op,
                    Value = ValueFromWire(e.Value, RunnerValue.OfBool(true)),
                });
            }
            return result;
        }

        // Narrow the boxed bool|long|string (the frozen union) Newtonsoft produces into a
        // RunnerValue. Newtonsoft deserializes JSON integers to Int64; int/double are tolerated for
        // robustness. A missing value falls back to the union's declared default.
        private static RunnerValue ValueFromWire(object boxed, RunnerValue fallback)
        {
            switch (boxed)
            {
                case null:
                    return fallback;
                case bool b:
                    return RunnerValue.OfBool(b);
                case long l:
                    return RunnerValue.OfInt(l);
                case int i:
                    return RunnerValue.OfInt(i);
                case double d:
                    return RunnerValue.OfInt((long)d);
                case string s:
                    return RunnerValue.OfString(s);
                default:
                    return RunnerValue.OfString(boxed.ToString());
            }
        }
    }
}
