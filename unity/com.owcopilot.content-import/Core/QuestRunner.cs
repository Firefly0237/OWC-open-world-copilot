// 任务状态机运行器：走一遍任务逻辑（simulate.py simulate_quest + _next_stage 的逐行移植，
// UnityEngine-free）。确定性、无模型调用；同输入同轨迹。
// A line-for-line port of the FROZEN reference oracle simulate.py::simulate_quest + _next_stage.
// The load-bearing behaviours that MUST match byte-for-byte (any deliberate divergence gets a ⚠):
//   * quest-level gate: before any stage runs, the quest-level logic.precondition is evaluated in the
//     seeded state; when false the run is "unavailable" (no stage entered, final_state = the seeded
//     state) — mirrors simulate_quest. Per-stage preconditions still gate below it.
//   * seeding: variable defaults, then every expression ref setdefault=false, then initial_state.
//   * effect apply order at a stage = stage.effects_on_complete THEN the taken branch's effects.
//   * branch selection = a queued+satisfiable choice first, else the FIRST satisfiable branch.
//   * cycle detection is stage-space (a revisited stage) and the revisit adds no step/path entry.
//   * terminal (empty to_stage) / deadlock (no satisfiable branch) / dangling (to_stage not a real
//     stage -> "blocked") / linear completion at the last stage; max_steps = 200.

using System.Collections.Generic;

namespace OWCopilot.ContentImport.Core
{
    public static class QuestRunner
    {
        public const int DefaultMaxSteps = 200;

        /// <summary>Walk a quest's logic deterministically, taking queued choices first then the
        /// first satisfiable branch, and report path / per-step trace / final state / outcome.
        /// Mirrors simulate_quest exactly.</summary>
        public static SimRun Simulate(
            RunnerQuest quest,
            IReadOnlyList<string> choices = null,
            IReadOnlyDictionary<string, RunnerValue> initialState = null,
            int maxSteps = DefaultMaxSteps)
        {
            RunnerQuestLogic logic = quest.Logic;
            var stages = new List<string>(quest.StageIds);
            var stageSet = new HashSet<string>(stages);
            if (logic == null || stages.Count == 0)
            {
                var trivial = new SimRun
                {
                    Status = "completed",
                    Message = "no logic layer; linear default completes",
                };
                trivial.Path.AddRange(stages);
                return trivial;
            }

            WorldState state = SeedState(logic, initialState);

            if (!QuestLogicEval.Truthy(state.Values, logic.Precondition))
            {
                // Quest-level gate: an unsatisfied precondition means no stage runs (mirrors
                // simulate_quest's "unavailable"). Message carries the gate expression verbatim.
                var unavailable = new SimRun
                {
                    Status = "unavailable",
                    Message = "quest precondition not met: " + logic.Precondition,
                };
                foreach (KeyValuePair<string, RunnerValue> kv in state.Values)
                {
                    unavailable.FinalState[kv.Key] = kv.Value;
                }
                return unavailable;
            }

            var stageLogic = new Dictionary<string, RunnerStageLogic>();
            foreach (RunnerStageLogic sl in logic.StageLogic)
            {
                stageLogic[sl.StageId] = sl; // last wins, matching the Python dict comprehension
            }
            var branchesFrom = new Dictionary<string, List<RunnerBranch>>();
            foreach (RunnerBranch branch in logic.Branches)
            {
                if (!branchesFrom.TryGetValue(branch.FromStage, out List<RunnerBranch> list))
                {
                    list = new List<RunnerBranch>();
                    branchesFrom[branch.FromStage] = list;
                }
                list.Add(branch);
            }

            var queued = choices != null ? new List<string>(choices) : new List<string>();
            var run = new SimRun { Status = "completed" };
            var visited = new HashSet<string>();
            string pos = stages[0];

            for (int step = 0; step < maxSteps; step++)
            {
                if (visited.Contains(pos))
                {
                    run.Status = "cycle";
                    run.Message = "cycle detected at stage '" + pos + "'";
                    break;
                }
                visited.Add(pos);
                stageLogic.TryGetValue(pos, out RunnerStageLogic sl);
                bool preconditionOk = sl == null || QuestLogicEval.Truthy(state.Values, sl.Precondition);
                var simStep = new SimStep { StageId = pos, PreconditionOk = preconditionOk };
                if (!preconditionOk)
                {
                    run.Steps.Add(simStep);
                    run.Path.Add(pos);
                    run.Status = "blocked";
                    run.Message = "precondition of stage '" + pos + "' is false";
                    break;
                }
                if (sl != null)
                {
                    foreach (RunnerEffect effect in sl.EffectsOnComplete)
                    {
                        state.Apply(effect.Var, effect.Op, effect.Value);
                        simStep.Effects.Add(EffectString(effect));
                    }
                }

                if (!branchesFrom.TryGetValue(pos, out List<RunnerBranch> outgoing))
                {
                    outgoing = EmptyBranches;
                }
                NextStage next = ResolveNext(outgoing, stages, pos, state, queued);
                if (next.Chosen != null)
                {
                    simStep.BranchTaken = next.Chosen.Id;
                    foreach (RunnerEffect effect in next.Chosen.Effects)
                    {
                        state.Apply(effect.Var, effect.Op, effect.Value);
                        simStep.Effects.Add(EffectString(effect));
                    }
                }
                run.Steps.Add(simStep);
                run.Path.Add(pos);
                if (next.Terminal)
                {
                    break;
                }
                if (next.NextStageId == null)
                {
                    run.Status = "deadlock";
                    run.Message = "stage '" + pos + "' has no takeable branch";
                    break;
                }
                if (!stageSet.Contains(next.NextStageId))
                {
                    run.Status = "blocked";
                    run.Message = "branch points at nonexistent stage '" + next.NextStageId + "'";
                    break;
                }
                pos = next.NextStageId;
            }

            foreach (KeyValuePair<string, RunnerValue> kv in state.Values)
            {
                run.FinalState[kv.Key] = kv.Value;
            }
            return run;
        }

        private static readonly List<RunnerBranch> EmptyBranches = new List<RunnerBranch>();

        // (next_stage_id, chosen_branch_or_null, is_terminal) — the _next_stage return triple.
        private struct NextStage
        {
            public string NextStageId;
            public RunnerBranch Chosen;
            public bool Terminal;
        }

        // Mirror _next_stage: selection must stay side-effect-free (the caller applies the chosen
        // branch's effects) so condition evaluation is not perturbed mid-decision.
        private static NextStage ResolveNext(
            List<RunnerBranch> outgoing,
            List<string> stages,
            string pos,
            WorldState state,
            List<string> queued)
        {
            if (outgoing.Count > 0)
            {
                RunnerBranch chosen = null;
                foreach (RunnerBranch branch in outgoing)
                {
                    if (queued.Contains(branch.Id) && QuestLogicEval.Truthy(state.Values, branch.Condition))
                    {
                        chosen = branch;
                        queued.Remove(branch.Id);
                        break;
                    }
                }
                if (chosen == null)
                {
                    foreach (RunnerBranch branch in outgoing)
                    {
                        if (QuestLogicEval.Truthy(state.Values, branch.Condition))
                        {
                            chosen = branch;
                            break;
                        }
                    }
                }
                if (chosen == null)
                {
                    return new NextStage { NextStageId = null, Chosen = null, Terminal = false };
                }
                if (!string.IsNullOrEmpty(chosen.ToStage))
                {
                    return new NextStage { NextStageId = chosen.ToStage, Chosen = chosen, Terminal = false };
                }
                return new NextStage { NextStageId = null, Chosen = chosen, Terminal = true };
            }
            int index = stages.IndexOf(pos);
            if (index + 1 < stages.Count)
            {
                return new NextStage { NextStageId = stages[index + 1], Chosen = null, Terminal = false };
            }
            return new NextStage { NextStageId = null, Chosen = null, Terminal = true };
        }

        // Seeding — mirror _seed_state: variable defaults, then every expression ref setdefault=false
        // (a broken expr is skipped), then initial_state overrides.
        private static WorldState SeedState(
            RunnerQuestLogic logic, IReadOnlyDictionary<string, RunnerValue> initial)
        {
            var state = new WorldState();
            foreach (RunnerLogicVar v in logic.Variables)
            {
                state.Values[v.Id] = v.Default;
            }
            foreach (string source in AllExpressions(logic))
            {
                try
                {
                    foreach (string reference in QuestLogicExpr.RefsIn(QuestLogicExpr.Parse(source)))
                    {
                        state.SetDefault(reference, RunnerValue.OfBool(false));
                    }
                }
                catch (LogicSyntaxException)
                {
                    // a broken expression contributes no refs (audit already flags it)
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

        private static IEnumerable<string> AllExpressions(RunnerQuestLogic logic)
        {
            var sources = new List<string>();
            if (!string.IsNullOrWhiteSpace(logic.Precondition))
            {
                sources.Add(logic.Precondition);
            }
            foreach (RunnerStageLogic sl in logic.StageLogic)
            {
                if (!string.IsNullOrWhiteSpace(sl.Precondition))
                {
                    sources.Add(sl.Precondition);
                }
            }
            foreach (RunnerBranch branch in logic.Branches)
            {
                if (!string.IsNullOrWhiteSpace(branch.Condition))
                {
                    sources.Add(branch.Condition);
                }
            }
            return sources;
        }

        // f"{var} {op} {value}" with Python str() rendering of the value (bool -> True/False).
        private static string EffectString(RunnerEffect effect) =>
            effect.Var + " " + effect.Op + " " + effect.Value.ToWireString();
    }
}
