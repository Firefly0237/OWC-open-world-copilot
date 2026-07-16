// 导入设置（EditorPrefs 持久化）：输出目录 + 是否随 content_bundle.json 改动自动重导。
// Editor-only, per-user settings. Auto-reimport defaults to OFF: engine imports stay human-gated
// (the same discipline as owcopilot.app.engine_sync — "engine edits never auto-land"). The
// AssetPostprocessor watcher only fires when the designer opts in via Tools > OWCopilot.

using UnityEditor;

namespace OWCopilot.ContentImport.Editor
{
    public static class ContentImportSettings
    {
        private const string OutputDirKey = "OWCopilot.ContentImport.OutputDir";
        private const string AutoReimportKey = "OWCopilot.ContentImport.AutoReimport";
        private const string LastBundlePathKey = "OWCopilot.ContentImport.LastBundlePath";
        private const string OrphanScanKey = "OWCopilot.ContentImport.OrphanScan";
        private const string OrphanDeleteKey = "OWCopilot.ContentImport.OrphanDelete";

        public const string DefaultOutputDir = "Assets/OWCopilot/Content";

        public static string OutputDir
        {
            get => EditorPrefs.GetString(OutputDirKey, DefaultOutputDir);
            set => EditorPrefs.SetString(OutputDirKey, string.IsNullOrEmpty(value) ? DefaultOutputDir : value);
        }

        public static bool AutoReimport
        {
            get => EditorPrefs.GetBool(AutoReimportKey, false);
            set => EditorPrefs.SetBool(AutoReimportKey, value);
        }

        public static string LastBundlePath
        {
            get => EditorPrefs.GetString(LastBundlePathKey, string.Empty);
            set => EditorPrefs.SetString(LastBundlePathKey, value ?? string.Empty);
        }

        // Orphan cleanup, opt-in and two-tiered (same human-gated discipline as auto-reimport):
        //   OrphanScan   OFF by default — when ON, an import lists assets whose id is no longer in the
        //                bundle (source-side deletions) into the ImportReport. Report only.
        //   OrphanDelete OFF by default and only meaningful with OrphanScan — when ON, the import
        //                additionally offers to DELETE those orphans, but never silently: a
        //                confirmation dialog gates every deletion.
        public static bool OrphanScan
        {
            get => EditorPrefs.GetBool(OrphanScanKey, false);
            set => EditorPrefs.SetBool(OrphanScanKey, value);
        }

        public static bool OrphanDelete
        {
            get => EditorPrefs.GetBool(OrphanDeleteKey, false);
            set => EditorPrefs.SetBool(OrphanDeleteKey, value);
        }
    }
}
