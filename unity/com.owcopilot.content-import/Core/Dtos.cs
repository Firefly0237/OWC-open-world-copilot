// content_bundle.json 的 Newtonsoft DTO 层（UnityEngine-free），字段名逐字对齐导出线格式。
// Newtonsoft DTOs for content_bundle.json. This assembly has NO UnityEngine reference so it can be
// unit-tested off-engine with `dotnet test`. JsonUtility cannot parse the bundle (it is dict-keyed
// {id: obj} and nested, and JsonUtility handles neither Dictionary nor nested polymorphic values),
// so we parse with the official com.unity.nuget.newtonsoft-json into these DTOs and map DTO -> SO
// explicitly in the Editor assembly. [JsonProperty] names are the exact snake_case wire keys emitted
// by src/owcopilot/exporters/json_bundle.py.
//
// LogicVar.default and Effect.value are the frozen bool|int|str union; Newtonsoft deserializes them
// into a boxed bool/long/string, kept here as `object` and narrowed to LogicValue on the SO side.

using System.Collections.Generic;
using Newtonsoft.Json;

namespace OWCopilot.ContentImport.Core
{
    public sealed class ContentBundleDto
    {
        [JsonProperty("entities")]
        public Dictionary<string, EntityDto> Entities = new Dictionary<string, EntityDto>();

        [JsonProperty("quests")]
        public Dictionary<string, QuestDto> Quests = new Dictionary<string, QuestDto>();

        [JsonProperty("dialogues")]
        public Dictionary<string, DialogueDto> Dialogues = new Dictionary<string, DialogueDto>();

        [JsonProperty("relations")]
        public List<RelationDto> Relations = new List<RelationDto>();

        // Additive: dialogue trees carry the branching-conversation graph the DialogueWalker runs.
        // Absent from older bundles (e.g. the E2 content_bundle.json) -> stays an empty dict, so no
        // existing consumer changes behaviour.
        [JsonProperty("dialogue_trees")]
        public Dictionary<string, DialogueTreeDto> DialogueTrees =
            new Dictionary<string, DialogueTreeDto>();
    }

    public sealed class EntityDto
    {
        [JsonProperty("id")] public string Id;
        [JsonProperty("name")] public string Name;
        [JsonProperty("type")] public string Type;
        [JsonProperty("description")] public string Description;
        [JsonProperty("aliases")] public List<string> Aliases = new List<string>();
        [JsonProperty("tags")] public List<string> Tags = new List<string>();
        [JsonProperty("status")] public string Status;
        [JsonProperty("origin")] public string Origin;
        [JsonProperty("review_status")] public string ReviewStatus;
    }

    public sealed class QuestDto
    {
        [JsonProperty("id")] public string Id;
        [JsonProperty("title")] public string Title;
        [JsonProperty("giver_npc")] public string GiverNpc;
        [JsonProperty("location")] public string Location;
        [JsonProperty("objective")] public string Objective;
        [JsonProperty("prerequisites")] public List<string> Prerequisites = new List<string>();
        [JsonProperty("timeline_order")] public int? TimelineOrder;
        [JsonProperty("dialogue_refs")] public List<string> DialogueRefs = new List<string>();
        [JsonProperty("stages")] public List<QuestStageDto> Stages = new List<QuestStageDto>();
        [JsonProperty("tags")] public List<string> Tags = new List<string>();
        [JsonProperty("logic")] public QuestLogicDto Logic;
        [JsonProperty("origin")] public string Origin;
        [JsonProperty("review_status")] public string ReviewStatus;
    }

    public sealed class QuestStageDto
    {
        [JsonProperty("id")] public string Id;
        [JsonProperty("summary")] public string Summary;
        [JsonProperty("location")] public string Location;
        [JsonProperty("required_entities")] public List<string> RequiredEntities = new List<string>();
    }

    public sealed class QuestLogicDto
    {
        [JsonProperty("variables")] public List<LogicVarDto> Variables = new List<LogicVarDto>();
        [JsonProperty("precondition")] public string Precondition;
        [JsonProperty("stage_logic")] public List<StageLogicDto> StageLogic = new List<StageLogicDto>();
        [JsonProperty("branches")] public List<BranchDto> Branches = new List<BranchDto>();
        [JsonProperty("unlocks")] public List<string> Unlocks = new List<string>();
    }

    public sealed class LogicVarDto
    {
        [JsonProperty("id")] public string Id;
        [JsonProperty("name")] public string Name;
        [JsonProperty("type")] public string Type;
        [JsonProperty("default")] public object Default; // bool | long | string (frozen union)
        [JsonProperty("enum_values")] public List<string> EnumValues = new List<string>();
    }

    public sealed class EffectDto
    {
        [JsonProperty("var")] public string Var;
        [JsonProperty("op")] public string Op;
        [JsonProperty("value")] public object Value; // bool | long | string (frozen union)
    }

    public sealed class StageLogicDto
    {
        [JsonProperty("stage_id")] public string StageId;
        [JsonProperty("precondition")] public string Precondition;
        [JsonProperty("effects_on_complete")] public List<EffectDto> EffectsOnComplete = new List<EffectDto>();
    }

    public sealed class BranchDto
    {
        [JsonProperty("id")] public string Id;
        [JsonProperty("from_stage")] public string FromStage;
        [JsonProperty("condition")] public string Condition;
        [JsonProperty("to_stage")] public string ToStage;
        [JsonProperty("outcome")] public string Outcome;
        [JsonProperty("effects")] public List<EffectDto> Effects = new List<EffectDto>();
    }

    public sealed class DialogueDto
    {
        [JsonProperty("id")] public string Id;
        [JsonProperty("text_key")] public string TextKey;
        [JsonProperty("speaker_id")] public string SpeakerId;
        [JsonProperty("quest_id")] public string QuestId;
        [JsonProperty("text")] public string Text;
        [JsonProperty("locale")] public string Locale;
        [JsonProperty("ui_max_len")] public int? UiMaxLen;
    }

    public sealed class RelationDto
    {
        [JsonProperty("source")] public string Source;
        [JsonProperty("target")] public string Target;
        [JsonProperty("kind")] public string Kind;
        [JsonProperty("valid_from")] public int? ValidFrom;
        [JsonProperty("valid_until")] public int? ValidUntil;
    }

    // --- dialogue trees (the branching-conversation graph walked by DialogueWalker) ---------------

    public sealed class DialogueTreeDto
    {
        [JsonProperty("id")] public string Id;
        [JsonProperty("quest_id")] public string QuestId;
        [JsonProperty("root_node")] public string RootNode;
        [JsonProperty("nodes")]
        public Dictionary<string, DialogueNodeDto> Nodes = new Dictionary<string, DialogueNodeDto>();
    }

    public sealed class DialogueNodeDto
    {
        [JsonProperty("id")] public string Id;
        [JsonProperty("speaker_id")] public string SpeakerId;
        [JsonProperty("text")] public string Text;
        [JsonProperty("choices")] public List<DialogueChoiceDto> Choices = new List<DialogueChoiceDto>();
        // Linear continuation when there are no (satisfiable) choices; null/absent = an ending.
        [JsonProperty("next_node")] public string NextNode;
    }

    public sealed class DialogueChoiceDto
    {
        [JsonProperty("text")] public string Text;
        [JsonProperty("next_node")] public string NextNode;
        [JsonProperty("condition")] public string Condition;
        // Empty effects are omitted on the wire (the Python model_serializer drops them); a missing
        // key round-trips back to an empty list here.
        [JsonProperty("effects")] public List<EffectDto> Effects = new List<EffectDto>();
    }
}
