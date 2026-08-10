// 导入结果：结构化的成功/失败 + 计数 + 引导式告警（供菜单/后处理器展示，不抛裸异常）。
// The structured result of an import — counts, resolved/unresolved reference totals, the honest
// warning list, and a guided error string on failure. Everything that can go wrong is surfaced here
// as data (Ok=false + Error), not as an exception thrown at the UI.

using System.Collections.Generic;

namespace OWCopilot.ContentImport.Editor
{
    public sealed class ImportReport
    {
        public bool Ok;
        public string Error;

        public int EntitiesImported;
        public int QuestsImported;
        public int DialoguesImported;
        public int DialogueTreesImported;
        public int RelationsImported;

        public int ResolvedReferences;
        public int UnresolvedReferences;

        public readonly List<string> Warnings = new List<string>();

        // Orphan cleanup (opt-in): assets present on disk under the tool's folders whose id is absent
        // from this import. Reported by default; deleted only after an explicit confirmation.
        public readonly List<string> Orphans = new List<string>();
        public int OrphansDeleted;

        public static ImportReport Failure(string error)
        {
            return new ImportReport { Ok = false, Error = error };
        }

        public string Summary()
        {
            if (!Ok)
            {
                return "内容包导入失败：" + Error;
            }
            return string.Format(
                "内容包导入完成：实体 {0}、任务 {1}、对白 {2}、对白树 {3}、关系 {4}；"
                + "引用已解析 {5}、待复核 {6}、告警 {7} 条{8}。",
                EntitiesImported, QuestsImported, DialoguesImported, DialogueTreesImported,
                RelationsImported, ResolvedReferences, UnresolvedReferences, Warnings.Count,
                OrphanSummary());
        }

        private string OrphanSummary()
        {
            if (Orphans.Count == 0)
            {
                return string.Empty;
            }
            return OrphansDeleted > 0
                ? $"；孤儿 {Orphans.Count}（已删 {OrphansDeleted}）"
                : $"；孤儿 {Orphans.Count}（仅报告）";
        }
    }
}
