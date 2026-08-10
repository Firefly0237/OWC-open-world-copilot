// 任务资产 + 原生任务逻辑层（变量/前置/效果/分支），对应 owcopilot.content.models 的
// Quest 与冻结的 WS-A 逻辑契约（QuestLogic/LogicVar/Effect/StageLogic/Branch）。
// The Quest ScriptableObject plus the native quest-logic layer. The logic types are [Serializable]
// nested classes (not separate .asset files): QuestLogic is nested inside a quest in the pydantic
// model, and Unity serializes nested [Serializable] classes in place. This keeps a generated
// "choice -> reputation/standing change" as structured data, never an unstructured string — the
// same reason the logic contract exists on the Python side.

using System;
using System.Collections.Generic;
using UnityEngine;

namespace OWCopilot.ContentImport
{
    /// <summary>Type of a quest/world-state variable — mirrors owcopilot LogicVarType.</summary>
    public enum LogicVarType
    {
        Bool,
        Int,
        Enum,
    }

    /// <summary>How an effect mutates a variable — mirrors owcopilot Effect.op.</summary>
    public enum EffectOp
    {
        Set,
        Inc,
        Dec,
    }

    /// <summary>Which arm of the bool|int|str union a <see cref="LogicValue"/> carries.</summary>
    public enum LogicValueKind
    {
        Bool,
        Int,
        String,
    }

    /// <summary>
    /// A faithful, inspector-serializable representation of the frozen <c>bool|int|str</c> union used
    /// by <c>LogicVar.default</c> and <c>Effect.value</c>. Unity can't serialize an <c>object</c>
    /// union, so the active arm is tagged by <see cref="Kind"/> and the other fields are inert.
    /// </summary>
    [Serializable]
    public struct LogicValue
    {
        public LogicValueKind Kind;
        public bool BoolValue;
        public long IntValue;
        public string StringValue;

        public static LogicValue OfBool(bool value) =>
            new LogicValue { Kind = LogicValueKind.Bool, BoolValue = value };

        public static LogicValue OfInt(long value) =>
            new LogicValue { Kind = LogicValueKind.Int, IntValue = value };

        public static LogicValue OfString(string value) =>
            new LogicValue { Kind = LogicValueKind.String, StringValue = value };
    }

    /// <summary>A quest/world-state variable the logic layer reads and writes (LogicVar).</summary>
    [Serializable]
    public sealed class LogicVar
    {
        public string Id;
        public string Name = "";
        public LogicVarType Type = LogicVarType.Bool;
        public LogicValue Default = LogicValue.OfBool(false);
        public List<string> EnumValues = new List<string>(); // only meaningful for Type == Enum
    }

    /// <summary>A mutation applied to a variable when a stage completes (Effect).</summary>
    [Serializable]
    public sealed class Effect
    {
        public string Var;
        public EffectOp Op = EffectOp.Set;
        public LogicValue Value = LogicValue.OfBool(true);
    }

    /// <summary>Per-stage precondition + on-complete effects (StageLogic).</summary>
    [Serializable]
    public sealed class StageLogic
    {
        public string StageId;
        public string Precondition = ""; // boolean expression source; empty = always enterable
        public List<Effect> EffectsOnComplete = new List<Effect>();
    }

    /// <summary>A branch out of a stage (Branch): taken when Condition holds.</summary>
    [Serializable]
    public sealed class Branch
    {
        public string Id;
        public string FromStage;
        public string Condition = "";
        public string ToStage = ""; // empty + Outcome set = a terminal outcome
        public string Outcome = "";
        public List<Effect> Effects = new List<Effect>();
    }

    /// <summary>The native quest logic/state layer (QuestLogic) — the single source of truth.</summary>
    [Serializable]
    public sealed class QuestLogic
    {
        public List<LogicVar> Variables = new List<LogicVar>();
        public string Precondition = ""; // quest-level gate
        public List<StageLogic> StageLogic = new List<StageLogic>();
        public List<Branch> Branches = new List<Branch>();
        public List<string> Unlocks = new List<string>(); // quest ids unlocked on completion
    }

    /// <summary>One quest stage (QuestStage). Location/required entities resolve to entity refs.</summary>
    [Serializable]
    public sealed class QuestStage
    {
        public string Id;
        public string Summary;

        public string LocationId;
        public EntitySO Location;

        public List<string> RequiredEntityIds = new List<string>();
        public List<EntitySO> RequiredEntities = new List<EntitySO>();
    }

    public sealed class QuestSO : ContentObjectSO
    {
        public string Title;

        [Tooltip("Giver NPC id (raw, authoritative).")]
        public string GiverNpcId;
        public EntitySO GiverNpc;

        [Tooltip("Location id (raw, authoritative).")]
        public string LocationId;
        public EntitySO Location;

        [TextArea(2, 6)]
        public string Objective;

        public List<string> PrerequisiteIds = new List<string>();
        public List<QuestSO> Prerequisites = new List<QuestSO>();

        // timeline_order is int|None on the wire.
        public bool HasTimelineOrder;
        public int TimelineOrder;

        public List<string> DialogueRefIds = new List<string>();
        public List<DialogueSO> DialogueRefs = new List<DialogueSO>();

        public List<QuestStage> Stages = new List<QuestStage>();
        public List<string> Tags = new List<string>();

        [Tooltip("Native logic layer (variables/preconditions/effects/branches). Null = legacy quest.")]
        public QuestLogic Logic;
    }
}
