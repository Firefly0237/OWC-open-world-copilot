// 运行器的领域模型 + 运行结果模型 + 冻结联合值 RunnerValue + 可变状态 WorldState（UnityEngine-free）。
// The runner's domain models (quest/dialogue graph, engine-free) and result models (SimRun /
// DialogueWalk), plus RunnerValue and WorldState — the C# side of the differential test. RunnerValue
// mirrors the Python Value = bool|int|str union; WorldState mirrors semantics.py WorldState.apply.
// Two equality notions are deliberately kept separate:
//   * PyEquals — the ==/!= semantics of semantics._compare: strings only equal strings, while bool
//     and int compare numerically (Python's bool-is-int quirk, True == 1). Used by the evaluator.
//   * Equals   — exact structural equality (same Kind AND value). Used to compare a produced
//     final_state against the golden trace, where Python preserves exact types.

using System;
using System.Collections.Generic;
using System.Globalization;

namespace OWCopilot.ContentImport.Core
{
    public enum RunnerValueKind
    {
        Bool,
        Int,
        String,
    }

    /// <summary>The frozen bool|int|str union a logic variable / literal carries. Int is
    /// <c>long</c> (Newtonsoft deserializes JSON integers to Int64) and all formatting uses
    /// InvariantCulture so numbers stringify identically to Python.</summary>
    public struct RunnerValue : IEquatable<RunnerValue>
    {
        public RunnerValueKind Kind;
        public bool Bool;
        public long Int;
        public string Str;

        public static RunnerValue OfBool(bool value) =>
            new RunnerValue { Kind = RunnerValueKind.Bool, Bool = value };

        public static RunnerValue OfInt(long value) =>
            new RunnerValue { Kind = RunnerValueKind.Int, Int = value };

        public static RunnerValue OfString(string value) =>
            new RunnerValue { Kind = RunnerValueKind.String, Str = value };

        /// <summary>Python truthiness — bool(value): bool as itself, int != 0, non-empty string.
        /// This is what simulate._truthy applies to a condition's evaluated result.</summary>
        public bool IsTruthy()
        {
            switch (Kind)
            {
                case RunnerValueKind.Bool:
                    return Bool;
                case RunnerValueKind.Int:
                    return Int != 0;
                default:
                    return !string.IsNullOrEmpty(Str);
            }
        }

        /// <summary>Python generic ==/!=: a string only equals a string; bool and int compare
        /// numerically (True == 1). Mirrors semantics._compare's "=="/"!=" arms.</summary>
        public bool PyEquals(RunnerValue other)
        {
            bool leftString = Kind == RunnerValueKind.String;
            bool rightString = other.Kind == RunnerValueKind.String;
            if (leftString || rightString)
            {
                return leftString && rightString && Str == other.Str;
            }
            return AsLong() == other.AsLong();
        }

        private long AsLong() => Kind == RunnerValueKind.Bool ? (Bool ? 1L : 0L) : Int;

        /// <summary>str(value) as Python renders it in the effect string f"{var} {op} {value}":
        /// bool -> True/False, int -> decimal (invariant), str -> raw.</summary>
        public string ToWireString()
        {
            switch (Kind)
            {
                case RunnerValueKind.Bool:
                    return Bool ? "True" : "False";
                case RunnerValueKind.Int:
                    return Int.ToString(CultureInfo.InvariantCulture);
                default:
                    return Str;
            }
        }

        public bool Equals(RunnerValue other)
        {
            if (Kind != other.Kind)
            {
                return false;
            }
            switch (Kind)
            {
                case RunnerValueKind.Bool:
                    return Bool == other.Bool;
                case RunnerValueKind.Int:
                    return Int == other.Int;
                default:
                    return Str == other.Str;
            }
        }

        public override bool Equals(object obj) => obj is RunnerValue value && Equals(value);

        public override int GetHashCode()
        {
            switch (Kind)
            {
                case RunnerValueKind.Bool:
                    return Bool ? 1 : 0;
                case RunnerValueKind.Int:
                    return Int.GetHashCode();
                default:
                    return Str == null ? 0 : Str.GetHashCode();
            }
        }
    }

    /// <summary>A mutable snapshot of logic variable values — mirrors semantics.py WorldState.</summary>
    public sealed class WorldState
    {
        public readonly Dictionary<string, RunnerValue> Values;

        public WorldState()
        {
            Values = new Dictionary<string, RunnerValue>();
        }

        public WorldState(IReadOnlyDictionary<string, RunnerValue> initial)
        {
            Values = new Dictionary<string, RunnerValue>();
            foreach (KeyValuePair<string, RunnerValue> kv in initial)
            {
                Values[kv.Key] = kv.Value;
            }
        }

        public void SetDefault(string var, RunnerValue value)
        {
            if (!Values.ContainsKey(var))
            {
                Values[var] = value;
            }
        }

        /// <summary>Apply an effect — byte-identical to WorldState.apply: "set" replaces; "inc"/"dec"
        /// add/subtract an int delta onto an int base (a non-int or missing current resets base to
        /// 0), and the result is always an int.</summary>
        public void Apply(string var, string op, RunnerValue value)
        {
            if (op == "set")
            {
                Values[var] = value;
                return;
            }
            long delta = ToLong(value);
            long baseValue =
                Values.TryGetValue(var, out RunnerValue current) && current.Kind == RunnerValueKind.Int
                    ? current.Int
                    : 0L;
            Values[var] = RunnerValue.OfInt(op == "inc" ? baseValue + delta : baseValue - delta);
        }

        // int(value): bool -> 0/1, int -> itself, string -> parsed decimal (mirrors Python int()).
        private static long ToLong(RunnerValue value)
        {
            switch (value.Kind)
            {
                case RunnerValueKind.Bool:
                    return value.Bool ? 1L : 0L;
                case RunnerValueKind.Int:
                    return value.Int;
                default:
                    return long.Parse(value.Str, CultureInfo.InvariantCulture);
            }
        }
    }

    // --- runner domain models (a bundle DTO / SO is projected into these; see RunnerModelFactory) --

    public sealed class RunnerEffect
    {
        public string Var;
        public string Op; // raw wire op: "set" | "inc" | "dec"
        public RunnerValue Value;
    }

    public sealed class RunnerLogicVar
    {
        public string Id;
        public RunnerValue Default;
    }

    public sealed class RunnerStageLogic
    {
        public string StageId;
        public string Precondition = "";
        public List<RunnerEffect> EffectsOnComplete = new List<RunnerEffect>();
    }

    public sealed class RunnerBranch
    {
        public string Id;
        public string FromStage;
        public string Condition = "";
        public string ToStage = ""; // empty + Outcome set = a terminal outcome
        public string Outcome = "";
        public List<RunnerEffect> Effects = new List<RunnerEffect>();
    }

    public sealed class RunnerQuestLogic
    {
        public List<RunnerLogicVar> Variables = new List<RunnerLogicVar>();
        public string Precondition = "";
        public List<RunnerStageLogic> StageLogic = new List<RunnerStageLogic>();
        public List<RunnerBranch> Branches = new List<RunnerBranch>();
    }

    public sealed class RunnerQuest
    {
        public string Id;
        public List<string> StageIds = new List<string>();
        public RunnerQuestLogic Logic; // null = legacy quest (no logic layer), completes trivially
    }

    public sealed class RunnerDialogueChoice
    {
        public string Condition = "";
        public string NextNode; // null = ends the tree when taken
        public List<RunnerEffect> Effects = new List<RunnerEffect>();
    }

    public sealed class RunnerDialogueNode
    {
        public string Id;
        public List<RunnerDialogueChoice> Choices = new List<RunnerDialogueChoice>();
        public string NextNode; // linear continuation when there are no satisfiable choices
    }

    public sealed class RunnerDialogueTree
    {
        public string Id;
        public string RootNode = "";
        public Dictionary<string, RunnerDialogueNode> Nodes =
            new Dictionary<string, RunnerDialogueNode>();
    }

    // --- result models (mirror simulate.SimStep/SimRun and dialogue.DialogueWalkStep/DialogueWalk) -

    public sealed class SimStep
    {
        public string StageId;
        public bool PreconditionOk;
        public List<string> Effects = new List<string>();
        public string BranchTaken = ""; // branch id, or "" for a linear advance / terminal
    }

    public sealed class SimRun
    {
        public string Status; // "completed" | "deadlock" | "blocked" | "cycle" | "unavailable"
        public List<string> Path = new List<string>();
        public List<SimStep> Steps = new List<SimStep>();
        public Dictionary<string, RunnerValue> FinalState = new Dictionary<string, RunnerValue>();
        public string Message = ""; // NOT compared cross-language (Chinese on the Python side)
    }

    public sealed class DialogueWalkStep
    {
        public string NodeId;
        public string Chosen = ""; // "<node_id>:<index>" of the taken choice, or ""
        public List<string> Effects = new List<string>();
    }

    public sealed class DialogueWalk
    {
        public string TreeId;
        public string Status; // "completed" | "dangling" | "deadlock" | "cycle"
        public List<string> Path = new List<string>();
        public List<DialogueWalkStep> Steps = new List<DialogueWalkStep>();
        public Dictionary<string, RunnerValue> FinalState = new Dictionary<string, RunnerValue>();
        public string Message = "";
    }
}
