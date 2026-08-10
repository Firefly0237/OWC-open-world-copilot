// 孤儿资产清理（opt-in）：对比本次导入的 id 集与磁盘上工具目录里既有的内容资产，找出源里已删除的孤儿。
// Finds OWCopilot content assets left on disk after a source-side deletion: an asset whose stable id is
// no longer present in the freshly imported bundle. Scanning is strictly scoped to the importer's own
// typed subfolders under the output dir — a content SO a designer hand-authored ANYWHERE ELSE is never
// even discovered, let alone deleted. Deletion is done only by the importer AFTER an explicit
// confirmation dialog (never silent); this class only locates candidates and, when told to, removes a
// vetted list. Editor-only (AssetDatabase) — unverified offline, same boundary as the rest of Editor/.

using System.Collections.Generic;
using UnityEditor;

namespace OWCopilot.ContentImport.Editor
{
    public static class OrphanCleanup
    {
        public struct Orphan
        {
            public string Category; // the typed subfolder: Entities / Quests / Dialogues / …
            public string Id;
            public string AssetPath;
        }

        /// <summary>
        /// Scan the importer's managed subfolders for content assets whose id is NOT in the set just
        /// imported. Only assets under those subfolders are candidates; anything elsewhere is invisible.
        /// </summary>
        public static List<Orphan> Scan(
            string outputDir,
            IReadOnlyCollection<string> liveEntityIds,
            IReadOnlyCollection<string> liveQuestIds,
            IReadOnlyCollection<string> liveDialogueIds,
            IReadOnlyCollection<string> liveDialogueTreeIds,
            IReadOnlyCollection<string> liveRelationIds)
        {
            var orphans = new List<Orphan>();
            ScanFolder<EntitySO>(outputDir, "Entities", liveEntityIds, orphans);
            ScanFolder<QuestSO>(outputDir, "Quests", liveQuestIds, orphans);
            ScanFolder<DialogueSO>(outputDir, "Dialogues", liveDialogueIds, orphans);
            ScanFolder<DialogueTreeSO>(outputDir, "DialogueTrees", liveDialogueTreeIds, orphans);
            ScanFolder<RelationSO>(outputDir, "Relations", liveRelationIds, orphans);
            return orphans;
        }

        /// <summary>Delete a vetted orphan list; append any path that fails to <paramref name="failures"/>.
        /// The caller is responsible for having confirmed with the user first — this never prompts.</summary>
        public static int Delete(IReadOnlyList<Orphan> orphans, List<string> failures)
        {
            int deleted = 0;
            foreach (Orphan orphan in orphans)
            {
                if (AssetDatabase.DeleteAsset(orphan.AssetPath))
                {
                    deleted++;
                }
                else
                {
                    failures.Add(orphan.AssetPath);
                }
            }
            return deleted;
        }

        private static void ScanFolder<T>(
            string outputDir, string sub, IReadOnlyCollection<string> liveIds, List<Orphan> orphans)
            where T : ContentObjectSO
        {
            string folder = outputDir.TrimEnd('/') + "/" + sub;
            if (!AssetDatabase.IsValidFolder(folder))
            {
                return; // nothing imported into this category yet -> no orphans possible
            }
            var live = new HashSet<string>(liveIds);
            // t:<TypeName> restricted to the single managed subfolder — the location IS the provenance
            // signal (this is the tool's own output area), so no other assets are ever considered.
            foreach (string guid in AssetDatabase.FindAssets("t:" + typeof(T).Name, new[] { folder }))
            {
                string path = AssetDatabase.GUIDToAssetPath(guid);
                T asset = AssetDatabase.LoadAssetAtPath<T>(path);
                if (asset == null)
                {
                    continue;
                }
                if (!live.Contains(asset.Id))
                {
                    orphans.Add(new Orphan { Category = sub, Id = asset.Id, AssetPath = path });
                }
            }
        }
    }
}
