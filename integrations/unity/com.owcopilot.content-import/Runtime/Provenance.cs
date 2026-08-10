// 内容对象的溯源与分类枚举 + 所有内容 SO 的共同基类。
// Provenance/classification enums + the shared base for every content ScriptableObject.
// These mirror the frozen pydantic contract in src/owcopilot/content/models.py (Origin,
// ReviewStatus, EntityType). Enum *names* are C#-cased; the wire form is lowercase snake_case and is
// mapped on import by the Editor assembly (Runtime stays free of parsing concerns).

using UnityEngine;

namespace OWCopilot.ContentImport
{
    /// <summary>Entity kind — mirrors owcopilot.content.models.EntityType (wire values lowercase).</summary>
    public enum EntityType
    {
        Npc,
        Location,
        Faction,
        Item,
        Event,
        Region,
        Organization,
        Concept,
        Term,
        Skill,
        Achievement,
    }

    /// <summary>Where a content object came from — mirrors owcopilot.content.models.Origin.</summary>
    public enum Origin
    {
        Human,
        AiDraft,
        AiPatch,
    }

    /// <summary>Human-review gate state — mirrors owcopilot.content.models.ReviewStatus.</summary>
    public enum ReviewStatus
    {
        Approved,
        PendingReview,
        Rejected,
    }

    /// <summary>
    /// Common base for every imported content asset. Carries the stable string <see cref="Id"/>
    /// (the foreign-key the two-pass resolver links on) plus provenance. Kept abstract so only the
    /// concrete typed assets (EntitySO/QuestSO/DialogueSO/RelationSO) are ever created.
    /// </summary>
    public abstract class ContentObjectSO : ScriptableObject
    {
        [Tooltip("Stable content id — the foreign key relations and references resolve against.")]
        public string Id;

        public Origin Origin = Origin.Human;
        public ReviewStatus ReviewStatus = ReviewStatus.Approved;
    }
}
