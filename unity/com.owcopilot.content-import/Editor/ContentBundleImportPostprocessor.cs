// 工程内 content_bundle.json 变化时自动重导（opt-in）。必须 static；带重入守卫防止无限重导。
// AssetPostprocessor watcher for an in-project content_bundle.json. Per the ScriptReference,
// OnPostprocessAllAssets MUST be static ("will not be called correctly if declared as an instance
// function"), and because the import it triggers writes new .asset files — which re-fire this
// callback — a reentrancy guard is mandatory. Auto-reimport is opt-in (ContentImportSettings, default
// off) so engine imports stay human-gated by default.

using System.IO;
using UnityEditor;
using UnityEngine;

namespace OWCopilot.ContentImport.Editor
{
    public sealed class ContentBundleImportPostprocessor : AssetPostprocessor
    {
        private const string BundleFileName = "content_bundle.json";
        private static bool s_importing;

        private static void OnPostprocessAllAssets(
            string[] importedAssets,
            string[] deletedAssets,
            string[] movedAssets,
            string[] movedFromAssetPaths,
            bool didDomainReload)
        {
            if (s_importing || !ContentImportSettings.AutoReimport)
            {
                return; // guard against the import writing assets and re-entering this callback
            }

            foreach (string path in importedAssets)
            {
                if (Path.GetFileName(path) != BundleFileName)
                {
                    continue;
                }
                try
                {
                    s_importing = true;
                    ImportReport report = ContentBundleImporter.ImportFromFile(path, ContentImportSettings.OutputDir);
                    Debug.Log("[OWCopilot] 自动重导 " + path + "：" + report.Summary());
                }
                finally
                {
                    s_importing = false;
                }
                break; // one bundle per pass is enough; a re-import re-scans anyway
            }
        }
    }
}
