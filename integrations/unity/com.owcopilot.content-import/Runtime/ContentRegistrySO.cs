// 内容注册表资产：把 id -> 资产 的映射落成可序列化列表（Unity 不能序列化 Dictionary）。
// The content registry: a single ScriptableObject holding id -> asset lookups for entities, quests
// and dialogues (as serialized Lists, because the Inspector/serializer can't persist a Dictionary),
// plus the flat relation list. The importer rebuilds it each run so the game gets O(1)-ish lookup
// without scanning the AssetDatabase. This is the "asset registry" pattern the design calls for.

using System.Collections.Generic;
using UnityEngine;

namespace OWCopilot.ContentImport
{
    public sealed class ContentRegistrySO : ScriptableObject
    {
        [System.Serializable]
        public struct EntityEntry
        {
            public string Id;
            public EntitySO Asset;
        }

        [System.Serializable]
        public struct QuestEntry
        {
            public string Id;
            public QuestSO Asset;
        }

        [System.Serializable]
        public struct DialogueEntry
        {
            public string Id;
            public DialogueSO Asset;
        }

        [System.Serializable]
        public struct DialogueTreeEntry
        {
            public string Id;
            public DialogueTreeSO Asset;
        }

        public List<EntityEntry> Entities = new List<EntityEntry>();
        public List<QuestEntry> Quests = new List<QuestEntry>();
        public List<DialogueEntry> Dialogues = new List<DialogueEntry>();
        public List<DialogueTreeEntry> DialogueTrees = new List<DialogueTreeEntry>();
        public List<RelationSO> Relations = new List<RelationSO>();

        public EntitySO FindEntity(string id)
        {
            foreach (var entry in Entities)
            {
                if (entry.Id == id)
                {
                    return entry.Asset;
                }
            }
            return null;
        }

        public QuestSO FindQuest(string id)
        {
            foreach (var entry in Quests)
            {
                if (entry.Id == id)
                {
                    return entry.Asset;
                }
            }
            return null;
        }

        public DialogueSO FindDialogue(string id)
        {
            foreach (var entry in Dialogues)
            {
                if (entry.Id == id)
                {
                    return entry.Asset;
                }
            }
            return null;
        }

        public DialogueTreeSO FindDialogueTree(string id)
        {
            foreach (var entry in DialogueTrees)
            {
                if (entry.Id == id)
                {
                    return entry.Asset;
                }
            }
            return null;
        }
    }
}
