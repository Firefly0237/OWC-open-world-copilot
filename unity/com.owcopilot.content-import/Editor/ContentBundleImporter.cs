// 两遍导入：把 content_bundle.json 落成原生 ScriptableObject 资产，并把字符串 id 引用接成对象引用。
// The two-pass importer. Pass 1 creates or *overwrites-in-place* one .asset per entity/quest/dialogue
// (and one per relation), building id -> SO maps. Pass 2 walks the resolver's links and assigns real
// object references; unresolved ids are kept on the asset (raw id fields) and reported, never
// fabricated. GUIDs survive re-import because existing assets are loaded and their fields overwritten
// rather than deleted+recreated — the property the design attributes to overwriting-in-place (we do
// NOT use JsonUtility.FromJsonOverwrite: the bundle is dict-keyed/snake_case and JsonUtility cannot
// target it; DTO -> SO mapping is explicit via Newtonsoft, see DEVIATION in docs/UNITY_INTEGRATION.md).

using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using OWCopilot.ContentImport.Core;
using UnityEditor;
using UnityEngine;

namespace OWCopilot.ContentImport.Editor
{
    public static class ContentBundleImporter
    {
        private const string EntitiesSub = "Entities";
        private const string QuestsSub = "Quests";
        private const string DialoguesSub = "Dialogues";
        private const string DialogueTreesSub = "DialogueTrees";
        private const string RelationsSub = "Relations";
        private const string RegistryAssetName = "ContentRegistry.asset";

        public static ImportReport ImportFromFile(string bundleJsonPath, string outputDir = null)
        {
            if (string.IsNullOrEmpty(bundleJsonPath) || !File.Exists(bundleJsonPath))
            {
                return ImportReport.Failure(
                    $"找不到内容包文件：{bundleJsonPath}。请选择 OWCopilot 导出的 content_bundle.json。");
            }

            string json;
            try
            {
                json = File.ReadAllText(bundleJsonPath);
            }
            catch (IOException ex)
            {
                return ImportReport.Failure($"读取 content_bundle.json 失败：{ex.Message}");
            }

            return ImportFromJson(json, outputDir);
        }

        public static ImportReport ImportFromJson(string json, string outputDir = null)
        {
            outputDir = NormalizeOutputDir(outputDir);

            ContentBundleDto bundle;
            try
            {
                bundle = ContentBundleParser.Parse(json);
            }
            catch (ContentBundleParseException ex)
            {
                return ImportReport.Failure(ex.Message);
            }

            ResolveResult resolve = ContentBundleResolver.Resolve(bundle);
            var report = new ImportReport();
            report.Warnings.AddRange(resolve.Warnings);
            report.ResolvedReferences = resolve.ResolvedLinkCount;
            report.UnresolvedReferences = resolve.UnresolvedLinkCount;

            EnsureAssetFolder(outputDir);
            EnsureAssetFolder(CombineAsset(outputDir, EntitiesSub));
            EnsureAssetFolder(CombineAsset(outputDir, QuestsSub));
            EnsureAssetFolder(CombineAsset(outputDir, DialoguesSub));
            EnsureAssetFolder(CombineAsset(outputDir, DialogueTreesSub));
            EnsureAssetFolder(CombineAsset(outputDir, RelationsSub));

            // Kept at method scope so the opt-in orphan sweep (after the import commits) can compare the
            // ids materialized this run against what is left on disk.
            var entityMap = new Dictionary<string, EntitySO>();
            var questMap = new Dictionary<string, QuestSO>();
            var dialogueMap = new Dictionary<string, DialogueSO>();
            var dialogueTreeMap = new Dictionary<string, DialogueTreeSO>();
            var usedRelationNames = new HashSet<string>();

            try
            {
                AssetDatabase.StartAssetEditing();

                // --- Pass 1: materialize one asset per content object, keyed by id ---
                foreach (KeyValuePair<string, EntityDto> kv in bundle.Entities)
                {
                    string id = string.IsNullOrEmpty(kv.Value.Id) ? kv.Key : kv.Value.Id;
                    EntitySO so = LoadOrCreate<EntitySO>(CombineAsset(outputDir, EntitiesSub), id);
                    MapEntity(kv.Value, id, so);
                    EditorUtility.SetDirty(so);
                    entityMap[id] = so;
                    report.EntitiesImported++;
                }

                foreach (KeyValuePair<string, QuestDto> kv in bundle.Quests)
                {
                    string id = string.IsNullOrEmpty(kv.Value.Id) ? kv.Key : kv.Value.Id;
                    QuestSO so = LoadOrCreate<QuestSO>(CombineAsset(outputDir, QuestsSub), id);
                    MapQuest(kv.Value, id, so);
                    EditorUtility.SetDirty(so);
                    questMap[id] = so;
                    report.QuestsImported++;
                }

                foreach (KeyValuePair<string, DialogueDto> kv in bundle.Dialogues)
                {
                    string id = string.IsNullOrEmpty(kv.Value.Id) ? kv.Key : kv.Value.Id;
                    DialogueSO so = LoadOrCreate<DialogueSO>(CombineAsset(outputDir, DialoguesSub), id);
                    MapDialogue(kv.Value, id, so);
                    EditorUtility.SetDirty(so);
                    dialogueMap[id] = so;
                    report.DialoguesImported++;
                }

                foreach (KeyValuePair<string, DialogueTreeDto> kv in bundle.DialogueTrees)
                {
                    string id = string.IsNullOrEmpty(kv.Value.Id) ? kv.Key : kv.Value.Id;
                    DialogueTreeSO so =
                        LoadOrCreate<DialogueTreeSO>(CombineAsset(outputDir, DialogueTreesSub), id);
                    MapDialogueTree(kv.Value, id, so);
                    EditorUtility.SetDirty(so);
                    dialogueTreeMap[id] = so;
                    report.DialogueTreesImported++;
                }

                // --- Pass 2: wire object references from resolved string ids ---
                foreach (KeyValuePair<string, QuestDto> kv in bundle.Quests)
                {
                    QuestDto dto = kv.Value;
                    string id = string.IsNullOrEmpty(dto.Id) ? kv.Key : dto.Id;
                    QuestSO so = questMap[id];
                    so.GiverNpc = Lookup(entityMap, dto.GiverNpc);
                    so.Location = Lookup(entityMap, dto.Location);
                    so.Prerequisites = ResolveList(dto.Prerequisites, questMap);
                    so.DialogueRefs = ResolveList(dto.DialogueRefs, dialogueMap);
                    WireStageRefs(dto, so, entityMap);
                    EditorUtility.SetDirty(so);
                }

                foreach (KeyValuePair<string, DialogueDto> kv in bundle.Dialogues)
                {
                    DialogueDto dto = kv.Value;
                    string id = string.IsNullOrEmpty(dto.Id) ? kv.Key : dto.Id;
                    DialogueSO so = dialogueMap[id];
                    so.Speaker = Lookup(entityMap, dto.SpeakerId);
                    so.Quest = Lookup(questMap, dto.QuestId);
                    EditorUtility.SetDirty(so);
                }

                foreach (KeyValuePair<string, DialogueTreeDto> kv in bundle.DialogueTrees)
                {
                    DialogueTreeDto dto = kv.Value;
                    string id = string.IsNullOrEmpty(dto.Id) ? kv.Key : dto.Id;
                    WireDialogueTreeRefs(dialogueTreeMap[id], id, dto, entityMap, questMap, report);
                    EditorUtility.SetDirty(dialogueTreeMap[id]);
                }

                var relationAssets = new List<RelationSO>();
                foreach (RelationDto dto in bundle.Relations)
                {
                    string assetName = UniqueRelationName(dto, usedRelationNames);
                    RelationSO so = LoadOrCreate<RelationSO>(CombineAsset(outputDir, RelationsSub), assetName);
                    MapRelation(dto, assetName, so, entityMap);
                    EditorUtility.SetDirty(so);
                    relationAssets.Add(so);
                    report.RelationsImported++;
                }

                RebuildRegistry(
                    outputDir, entityMap, questMap, dialogueMap, dialogueTreeMap, relationAssets);
            }
            finally
            {
                AssetDatabase.StopAssetEditing();
            }

            AssetDatabase.SaveAssets();
            AssetDatabase.Refresh();

            // Opt-in orphan sweep runs after the import has committed, so the id sets are final and the
            // AssetDatabase is current for the folder scan.
            HandleOrphans(report, outputDir, entityMap, questMap, dialogueMap, dialogueTreeMap,
                usedRelationNames);

            report.Ok = true;
            return report;
        }

        // --- DTO -> SO mapping --------------------------------------------------------------------

        private static void MapEntity(EntityDto dto, string id, EntitySO so)
        {
            so.Id = id;
            so.DisplayName = dto.Name ?? id;
            so.Type = ParseEntityType(dto.Type);
            so.Description = dto.Description ?? string.Empty;
            so.Aliases = dto.Aliases ?? new List<string>();
            so.Tags = dto.Tags ?? new List<string>();
            so.Status = string.IsNullOrEmpty(dto.Status) ? "active" : dto.Status;
            so.Origin = ParseOrigin(dto.Origin);
            so.ReviewStatus = ParseReviewStatus(dto.ReviewStatus);
        }

        private static void MapQuest(QuestDto dto, string id, QuestSO so)
        {
            so.Id = id;
            so.Title = dto.Title ?? id;
            so.GiverNpcId = dto.GiverNpc;
            so.LocationId = dto.Location;
            so.Objective = dto.Objective ?? string.Empty;
            so.PrerequisiteIds = dto.Prerequisites ?? new List<string>();
            so.DialogueRefIds = dto.DialogueRefs ?? new List<string>();
            so.Tags = dto.Tags ?? new List<string>();
            so.HasTimelineOrder = dto.TimelineOrder.HasValue;
            so.TimelineOrder = dto.TimelineOrder ?? 0;
            so.Stages = MapStages(dto.Stages);
            so.Logic = MapLogic(dto.Logic);
            so.Origin = ParseOrigin(dto.Origin);
            so.ReviewStatus = ParseReviewStatus(dto.ReviewStatus);
            // Object references are wired in pass 2; leave them cleared so a re-import doesn't keep a
            // now-dangling link.
            so.GiverNpc = null;
            so.Location = null;
            so.Prerequisites = new List<QuestSO>();
            so.DialogueRefs = new List<DialogueSO>();
        }

        private static List<QuestStage> MapStages(List<QuestStageDto> stages)
        {
            var result = new List<QuestStage>();
            if (stages == null)
            {
                return result;
            }
            foreach (QuestStageDto s in stages)
            {
                result.Add(new QuestStage
                {
                    Id = s.Id,
                    Summary = s.Summary ?? string.Empty,
                    LocationId = s.Location,
                    RequiredEntityIds = s.RequiredEntities ?? new List<string>(),
                    RequiredEntities = new List<EntitySO>(),
                });
            }
            return result;
        }

        private static QuestLogic MapLogic(QuestLogicDto dto)
        {
            if (dto == null)
            {
                return null; // None on the wire = legacy quest, behaviour unchanged
            }
            var logic = new QuestLogic
            {
                Precondition = dto.Precondition ?? string.Empty,
                Unlocks = dto.Unlocks ?? new List<string>(),
            };
            if (dto.Variables != null)
            {
                foreach (LogicVarDto v in dto.Variables)
                {
                    logic.Variables.Add(new LogicVar
                    {
                        Id = v.Id,
                        Name = v.Name ?? string.Empty,
                        Type = ParseLogicVarType(v.Type),
                        Default = ToLogicValue(v.Default, false),
                        EnumValues = v.EnumValues ?? new List<string>(),
                    });
                }
            }
            if (dto.StageLogic != null)
            {
                foreach (StageLogicDto s in dto.StageLogic)
                {
                    logic.StageLogic.Add(new StageLogic
                    {
                        StageId = s.StageId,
                        Precondition = s.Precondition ?? string.Empty,
                        EffectsOnComplete = MapEffects(s.EffectsOnComplete),
                    });
                }
            }
            if (dto.Branches != null)
            {
                foreach (BranchDto b in dto.Branches)
                {
                    logic.Branches.Add(new Branch
                    {
                        Id = b.Id,
                        FromStage = b.FromStage,
                        Condition = b.Condition ?? string.Empty,
                        ToStage = b.ToStage ?? string.Empty,
                        Outcome = b.Outcome ?? string.Empty,
                        Effects = MapEffects(b.Effects),
                    });
                }
            }
            return logic;
        }

        private static List<Effect> MapEffects(List<EffectDto> effects)
        {
            var result = new List<Effect>();
            if (effects == null)
            {
                return result;
            }
            foreach (EffectDto e in effects)
            {
                result.Add(new Effect
                {
                    Var = e.Var,
                    Op = ParseEffectOp(e.Op),
                    Value = ToLogicValue(e.Value, true),
                });
            }
            return result;
        }

        private static void MapDialogue(DialogueDto dto, string id, DialogueSO so)
        {
            so.Id = id;
            so.TextKey = dto.TextKey;
            so.SpeakerId = dto.SpeakerId;
            so.QuestId = dto.QuestId;
            so.Text = dto.Text;
            so.Locale = dto.Locale;
            so.HasUiMaxLen = dto.UiMaxLen.HasValue;
            so.UiMaxLen = dto.UiMaxLen ?? 0;
            so.Speaker = null;
            so.Quest = null;
        }

        // Pass 1 for a dialogue tree: raw ids + the node/choice graph. Object references (Quest, per-node
        // Speaker, and the intra-tree choice/next node links) are wired in pass 2 by WireDialogueTreeRefs.
        private static void MapDialogueTree(DialogueTreeDto dto, string id, DialogueTreeSO so)
        {
            so.Id = id;
            so.QuestId = dto.QuestId;
            so.RootNodeId = dto.RootNode ?? string.Empty;
            so.Nodes = MapTreeNodes(dto.Nodes);
            so.Quest = null;
        }

        private static List<DialogueTreeNode> MapTreeNodes(Dictionary<string, DialogueNodeDto> nodes)
        {
            var result = new List<DialogueTreeNode>();
            if (nodes == null)
            {
                return result;
            }
            foreach (KeyValuePair<string, DialogueNodeDto> kv in nodes)
            {
                DialogueNodeDto dto = kv.Value;
                result.Add(new DialogueTreeNode
                {
                    Id = string.IsNullOrEmpty(dto.Id) ? kv.Key : dto.Id,
                    SpeakerId = dto.SpeakerId,
                    Speaker = null,
                    Text = dto.Text ?? string.Empty,
                    Choices = MapTreeChoices(dto.Choices),
                    NextNodeId = dto.NextNode,
                    NextNode = null,
                });
            }
            return result;
        }

        private static List<DialogueTreeChoice> MapTreeChoices(List<DialogueChoiceDto> choices)
        {
            var result = new List<DialogueTreeChoice>();
            if (choices == null)
            {
                return result;
            }
            foreach (DialogueChoiceDto dto in choices)
            {
                result.Add(new DialogueTreeChoice
                {
                    Text = dto.Text ?? string.Empty,
                    NextNodeId = dto.NextNode,
                    NextNode = null,
                    Condition = dto.Condition ?? string.Empty,
                    Effects = MapEffects(dto.Effects),
                });
            }
            return result;
        }

        // Pass 2 for a dialogue tree: cross-object refs (quest, per-node speaker) resolve against the
        // bundle-wide maps; intra-tree choice/next links resolve against the tree's OWN node id map.
        // Every unresolved link keeps its raw id and is reported — never fabricated.
        private static void WireDialogueTreeRefs(
            DialogueTreeSO so, string treeId, DialogueTreeDto dto,
            Dictionary<string, EntitySO> entityMap, Dictionary<string, QuestSO> questMap,
            ImportReport report)
        {
            so.Quest = Lookup(questMap, dto.QuestId);
            Dictionary<string, DialogueTreeNode> nodeById = BuildNodeMap(so.Nodes);
            foreach (DialogueTreeNode node in so.Nodes)
            {
                node.Speaker = Lookup(entityMap, node.SpeakerId);
                node.NextNode = LookupNode(nodeById, node.NextNodeId, treeId, node.Id, "next_node", report);
                foreach (DialogueTreeChoice choice in node.Choices)
                {
                    choice.NextNode =
                        LookupNode(nodeById, choice.NextNodeId, treeId, node.Id, "choice", report);
                }
            }
        }

        private static Dictionary<string, DialogueTreeNode> BuildNodeMap(List<DialogueTreeNode> nodes)
        {
            var map = new Dictionary<string, DialogueTreeNode>();
            foreach (DialogueTreeNode node in nodes)
            {
                if (!string.IsNullOrEmpty(node.Id))
                {
                    map[node.Id] = node; // last wins on a duplicate id, mirroring dict-keyed nodes
                }
            }
            return map;
        }

        private static DialogueTreeNode LookupNode(
            Dictionary<string, DialogueTreeNode> nodeById, string targetId, string treeId,
            string fromNode, string field, ImportReport report)
        {
            if (string.IsNullOrEmpty(targetId))
            {
                return null; // empty = ending / no linear continuation (an intentional terminal)
            }
            if (nodeById.TryGetValue(targetId, out DialogueTreeNode node))
            {
                return node;
            }
            report.Warnings.Add(
                $"对白树「{treeId}」节点「{fromNode}」的 {field} 指向未知节点「{targetId}」，保留待人审复核");
            return null;
        }

        private static void MapRelation(
            RelationDto dto, string id, RelationSO so, Dictionary<string, EntitySO> entityMap)
        {
            so.Id = id;
            so.SourceId = dto.Source;
            so.TargetId = dto.Target;
            so.Kind = dto.Kind;
            so.Source = Lookup(entityMap, dto.Source);
            so.Target = Lookup(entityMap, dto.Target);
            so.HasValidFrom = dto.ValidFrom.HasValue;
            so.ValidFrom = dto.ValidFrom ?? 0;
            so.HasValidUntil = dto.ValidUntil.HasValue;
            so.ValidUntil = dto.ValidUntil ?? 0;
        }

        private static void WireStageRefs(QuestDto dto, QuestSO so, Dictionary<string, EntitySO> entityMap)
        {
            if (dto.Stages == null)
            {
                return;
            }
            for (int i = 0; i < so.Stages.Count && i < dto.Stages.Count; i++)
            {
                QuestStage stage = so.Stages[i];
                stage.Location = Lookup(entityMap, dto.Stages[i].Location);
                stage.RequiredEntities = ResolveList(dto.Stages[i].RequiredEntities, entityMap);
            }
        }

        // --- reference resolution helpers ---------------------------------------------------------

        private static T Lookup<T>(Dictionary<string, T> map, string id) where T : class
        {
            if (string.IsNullOrEmpty(id))
            {
                return null;
            }
            return map.TryGetValue(id, out T value) ? value : null;
        }

        private static List<T> ResolveList<T>(List<string> ids, Dictionary<string, T> map) where T : class
        {
            var result = new List<T>();
            if (ids == null)
            {
                return result;
            }
            foreach (string id in ids)
            {
                T value = Lookup(map, id);
                if (value != null)
                {
                    result.Add(value); // unresolved ids stay in the *Ids list; refs only hold resolved
                }
            }
            return result;
        }

        // --- registry -----------------------------------------------------------------------------

        private static void RebuildRegistry(
            string outputDir,
            Dictionary<string, EntitySO> entityMap,
            Dictionary<string, QuestSO> questMap,
            Dictionary<string, DialogueSO> dialogueMap,
            Dictionary<string, DialogueTreeSO> dialogueTreeMap,
            List<RelationSO> relations)
        {
            string path = CombineAsset(outputDir, RegistryAssetName);
            ContentRegistrySO registry = AssetDatabase.LoadAssetAtPath<ContentRegistrySO>(path);
            if (registry == null)
            {
                registry = ScriptableObject.CreateInstance<ContentRegistrySO>();
                AssetDatabase.CreateAsset(registry, path);
            }
            registry.Entities = new List<ContentRegistrySO.EntityEntry>();
            registry.Quests = new List<ContentRegistrySO.QuestEntry>();
            registry.Dialogues = new List<ContentRegistrySO.DialogueEntry>();
            registry.DialogueTrees = new List<ContentRegistrySO.DialogueTreeEntry>();
            registry.Relations = new List<RelationSO>(relations);
            foreach (KeyValuePair<string, EntitySO> kv in entityMap)
            {
                registry.Entities.Add(new ContentRegistrySO.EntityEntry { Id = kv.Key, Asset = kv.Value });
            }
            foreach (KeyValuePair<string, QuestSO> kv in questMap)
            {
                registry.Quests.Add(new ContentRegistrySO.QuestEntry { Id = kv.Key, Asset = kv.Value });
            }
            foreach (KeyValuePair<string, DialogueSO> kv in dialogueMap)
            {
                registry.Dialogues.Add(new ContentRegistrySO.DialogueEntry { Id = kv.Key, Asset = kv.Value });
            }
            foreach (KeyValuePair<string, DialogueTreeSO> kv in dialogueTreeMap)
            {
                registry.DialogueTrees.Add(
                    new ContentRegistrySO.DialogueTreeEntry { Id = kv.Key, Asset = kv.Value });
            }
            EditorUtility.SetDirty(registry);
        }

        // --- orphan cleanup (opt-in, two-tiered, never silent) ------------------------------------

        private static void HandleOrphans(
            ImportReport report, string outputDir,
            Dictionary<string, EntitySO> entityMap, Dictionary<string, QuestSO> questMap,
            Dictionary<string, DialogueSO> dialogueMap, Dictionary<string, DialogueTreeSO> dialogueTreeMap,
            HashSet<string> relationNames)
        {
            if (!ContentImportSettings.OrphanScan)
            {
                return; // scanning is opt-in; default import never looks at other assets
            }
            List<OrphanCleanup.Orphan> orphans = OrphanCleanup.Scan(
                outputDir, entityMap.Keys, questMap.Keys, dialogueMap.Keys, dialogueTreeMap.Keys,
                relationNames);
            foreach (OrphanCleanup.Orphan orphan in orphans)
            {
                report.Orphans.Add($"{orphan.Category}/{orphan.Id}  ({orphan.AssetPath})");
            }
            if (orphans.Count == 0 || !ContentImportSettings.OrphanDelete)
            {
                return; // report-only: the default, or delete disabled, or nothing to delete
            }
            // Second gate: even with delete enabled, deletion is NEVER silent — confirm first.
            bool confirmed = EditorUtility.DisplayDialog(
                "OWCopilot 孤儿资产清理",
                $"本次导入未包含以下 {orphans.Count} 个既有资产（源数据里已删除）。\n"
                + "删除不可撤销（可用版本控制找回）。是否删除？\n\n"
                + OrphanPreview(orphans),
                "删除", "只报告不删");
            if (!confirmed)
            {
                return;
            }
            var failures = new List<string>();
            report.OrphansDeleted = OrphanCleanup.Delete(orphans, failures);
            foreach (string path in failures)
            {
                report.Warnings.Add($"孤儿资产删除失败（已保留）：{path}");
            }
            AssetDatabase.SaveAssets();
            AssetDatabase.Refresh();
        }

        private static string OrphanPreview(List<OrphanCleanup.Orphan> orphans)
        {
            const int max = 12;
            var sb = new StringBuilder();
            for (int i = 0; i < orphans.Count && i < max; i++)
            {
                sb.Append("• ").Append(orphans[i].Category).Append('/').Append(orphans[i].Id)
                    .Append('\n');
            }
            if (orphans.Count > max)
            {
                sb.Append("…（其余 ").Append(orphans.Count - max).Append(" 个略）");
            }
            return sb.ToString();
        }

        // --- asset I/O (GUID-preserving) ----------------------------------------------------------

        private static T LoadOrCreate<T>(string folderAssetPath, string id) where T : ScriptableObject
        {
            string path = CombineAsset(folderAssetPath, SanitizeFileName(id) + ".asset");
            T existing = AssetDatabase.LoadAssetAtPath<T>(path);
            if (existing != null)
            {
                return existing; // overwrite in place -> GUID preserved, inbound refs survive
            }
            T created = ScriptableObject.CreateInstance<T>();
            AssetDatabase.CreateAsset(created, path);
            return created;
        }

        private static string UniqueRelationName(RelationDto dto, HashSet<string> used)
        {
            string baseName = SanitizeFileName(
                $"rel_{dto.Source}_{dto.Kind}_{dto.Target}");
            string name = baseName;
            int n = 2;
            while (!used.Add(name))
            {
                name = baseName + "_" + n;
                n++;
            }
            return name;
        }

        // --- folder + name utilities --------------------------------------------------------------

        private static string NormalizeOutputDir(string outputDir)
        {
            if (string.IsNullOrEmpty(outputDir))
            {
                return ContentImportSettings.OutputDir;
            }
            return outputDir.Replace('\\', '/').TrimEnd('/');
        }

        private static string CombineAsset(string parent, string child)
        {
            return parent.TrimEnd('/') + "/" + child;
        }

        /// <summary>Create every missing folder along an "Assets/a/b/c" path via AssetDatabase.</summary>
        private static void EnsureAssetFolder(string assetPath)
        {
            if (AssetDatabase.IsValidFolder(assetPath))
            {
                return;
            }
            string[] parts = assetPath.Split('/');
            if (parts.Length == 0 || parts[0] != "Assets")
            {
                // Outside Assets/ we can only rely on the OS; create the directory so writes succeed.
                Directory.CreateDirectory(assetPath);
                return;
            }
            string current = "Assets";
            for (int i = 1; i < parts.Length; i++)
            {
                string next = current + "/" + parts[i];
                if (!AssetDatabase.IsValidFolder(next))
                {
                    AssetDatabase.CreateFolder(current, parts[i]);
                }
                current = next;
            }
        }

        private static string SanitizeFileName(string id)
        {
            if (string.IsNullOrEmpty(id))
            {
                return "_";
            }
            var chars = id.ToCharArray();
            for (int i = 0; i < chars.Length; i++)
            {
                char c = chars[i];
                bool ok = c == '_' || c == '-' || c == '.' ||
                          (c >= '0' && c <= '9') || (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
                          c > 127; // keep CJK and other non-ASCII identifiers intact
                if (!ok)
                {
                    chars[i] = '_';
                }
            }
            return new string(chars);
        }

        // --- enum + union parsing -----------------------------------------------------------------

        private static EntityType ParseEntityType(string wire) =>
            ParseEnum(wire, EntityType.Concept);

        private static Origin ParseOrigin(string wire) =>
            ParseEnum(wire, Origin.Human);

        private static ReviewStatus ParseReviewStatus(string wire) =>
            ParseEnum(wire, ReviewStatus.Approved);

        private static LogicVarType ParseLogicVarType(string wire) =>
            ParseEnum(wire, LogicVarType.Bool);

        private static EffectOp ParseEffectOp(string wire) =>
            ParseEnum(wire, EffectOp.Set);

        /// <summary>Map a lowercase snake_case wire enum to a C#-cased enum (underscore-insensitive).</summary>
        private static T ParseEnum<T>(string wire, T fallback) where T : struct, Enum
        {
            if (string.IsNullOrEmpty(wire))
            {
                return fallback;
            }
            string normalized = wire.Replace("_", string.Empty);
            return Enum.TryParse(normalized, ignoreCase: true, out T parsed) ? parsed : fallback;
        }

        /// <summary>Narrow a boxed bool|long|string (the frozen union) into a LogicValue.</summary>
        private static LogicValue ToLogicValue(object boxed, bool defaultBool)
        {
            switch (boxed)
            {
                case null:
                    return LogicValue.OfBool(defaultBool);
                case bool b:
                    return LogicValue.OfBool(b);
                case long l:
                    return LogicValue.OfInt(l);
                case int i:
                    return LogicValue.OfInt(i);
                case double d:
                    return LogicValue.OfInt((long)d);
                case string s:
                    return LogicValue.OfString(s);
                default:
                    return LogicValue.OfString(boxed.ToString());
            }
        }
    }
}
