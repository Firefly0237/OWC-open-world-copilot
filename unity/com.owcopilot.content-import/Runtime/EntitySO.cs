// 实体资产：NPC / 地点 / 阵营… 对应 owcopilot.content.models.Entity。
// The Entity ScriptableObject — the target of most cross-references (giver_npc, location,
// relation source/target). Plain data carrier; the importer fills it, the game reads it.

using System.Collections.Generic;
using UnityEngine;

namespace OWCopilot.ContentImport
{
    public sealed class EntitySO : ContentObjectSO
    {
        public string DisplayName;
        public EntityType Type = EntityType.Concept;

        [TextArea(2, 6)]
        public string Description;

        public List<string> Aliases = new List<string>();
        public List<string> Tags = new List<string>();
        public string Status = "active";
    }
}
