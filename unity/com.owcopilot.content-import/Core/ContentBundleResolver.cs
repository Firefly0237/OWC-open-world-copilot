// 两遍导入的第一/第二遍公用的“外键解析”核心（UnityEngine-free，离线可测）。
// The load-bearing two-pass resolution logic, kept engine-free so `dotnet test` can exercise it off
// a Unity machine. It walks every string-id reference in the bundle (quest.giver_npc / location /
// prerequisites / dialogue_refs / stage locations+required_entities, dialogue.speaker_id/quest_id,
// relation.source/target) and records, per reference, whether the target id exists in the right
// universe (entities / quests / dialogues). Unresolved ids are NOT fabricated: they are kept and
// reported with the exact warning template used by src/owcopilot/recognize/engine_data.py
// (「行「{source}」引用未知对象「{target}」，保留待人审复核」), so both engines behave identically.
//
// The Editor importer consumes ResolveResult to wire real object references in pass 2; the raw ids
// stay authoritative on the assets regardless of resolution.

using System.Collections.Generic;

namespace OWCopilot.ContentImport.Core
{
    /// <summary>Which id-keyed collection a reference points into.</summary>
    public enum ContentKind
    {
        Entity,
        Quest,
        Dialogue,
    }

    /// <summary>One id reference discovered in the bundle and whether it resolves.</summary>
    public sealed class ReferenceLink
    {
        public string OwnerId;
        public string Field;
        public ContentKind TargetKind;
        public string TargetId;
        public bool Resolved;
    }

    public sealed class ResolveResult
    {
        public ContentBundleDto Bundle;
        public readonly List<ReferenceLink> Links = new List<ReferenceLink>();
        public readonly List<string> Warnings = new List<string>();

        public int EntityCount => Bundle.Entities.Count;
        public int QuestCount => Bundle.Quests.Count;
        public int DialogueCount => Bundle.Dialogues.Count;
        public int RelationCount => Bundle.Relations.Count;

        public int ResolvedLinkCount
        {
            get
            {
                int n = 0;
                foreach (var link in Links)
                {
                    if (link.Resolved)
                    {
                        n++;
                    }
                }
                return n;
            }
        }

        public int UnresolvedLinkCount => Links.Count - ResolvedLinkCount;
    }

    public static class ContentBundleResolver
    {
        public static ResolveResult Resolve(ContentBundleDto bundle)
        {
            var result = new ResolveResult { Bundle = bundle };
            var entityIds = new HashSet<string>(bundle.Entities.Keys);
            var questIds = new HashSet<string>(bundle.Quests.Keys);
            var dialogueIds = new HashSet<string>(bundle.Dialogues.Keys);

            void Link(string owner, string field, ContentKind kind, string target, HashSet<string> universe)
            {
                if (string.IsNullOrEmpty(target))
                {
                    return;
                }
                bool resolved = universe.Contains(target);
                result.Links.Add(new ReferenceLink
                {
                    OwnerId = owner,
                    Field = field,
                    TargetKind = kind,
                    TargetId = target,
                    Resolved = resolved,
                });
                if (!resolved)
                {
                    result.Warnings.Add($"行「{owner}」引用未知对象「{target}」，保留待人审复核");
                }
            }

            foreach (KeyValuePair<string, QuestDto> kv in bundle.Quests)
            {
                QuestDto quest = kv.Value;
                string owner = string.IsNullOrEmpty(quest.Id) ? kv.Key : quest.Id;
                Link(owner, "giver_npc", ContentKind.Entity, quest.GiverNpc, entityIds);
                Link(owner, "location", ContentKind.Entity, quest.Location, entityIds);
                if (quest.Prerequisites != null)
                {
                    foreach (string pre in quest.Prerequisites)
                    {
                        Link(owner, "prerequisites", ContentKind.Quest, pre, questIds);
                    }
                }
                if (quest.DialogueRefs != null)
                {
                    foreach (string dref in quest.DialogueRefs)
                    {
                        Link(owner, "dialogue_refs", ContentKind.Dialogue, dref, dialogueIds);
                    }
                }
                if (quest.Stages != null)
                {
                    foreach (QuestStageDto stage in quest.Stages)
                    {
                        Link(owner, "stage.location", ContentKind.Entity, stage.Location, entityIds);
                        if (stage.RequiredEntities != null)
                        {
                            foreach (string re in stage.RequiredEntities)
                            {
                                Link(owner, "stage.required_entities", ContentKind.Entity, re, entityIds);
                            }
                        }
                    }
                }
            }

            foreach (KeyValuePair<string, DialogueDto> kv in bundle.Dialogues)
            {
                DialogueDto dialogue = kv.Value;
                string owner = string.IsNullOrEmpty(dialogue.Id) ? kv.Key : dialogue.Id;
                Link(owner, "speaker_id", ContentKind.Entity, dialogue.SpeakerId, entityIds);
                Link(owner, "quest_id", ContentKind.Quest, dialogue.QuestId, questIds);
            }

            foreach (RelationDto relation in bundle.Relations)
            {
                // Mirror engine_data.py: an unresolved source or target is kept and flagged, never
                // invented. The owner is the relation source (matching the Chinese warning subject).
                Link(relation.Source, "source", ContentKind.Entity, relation.Source, entityIds);
                Link(relation.Source, "target", ContentKind.Entity, relation.Target, entityIds);
            }

            return result;
        }
    }
}
