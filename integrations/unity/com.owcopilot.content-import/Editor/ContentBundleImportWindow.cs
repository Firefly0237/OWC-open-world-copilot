// 编辑器内导入界面：Tools > OWCopilot 菜单 + 一个小窗口（选 content_bundle.json、选输出目录、导入）。
// The in-editor UX surface. A [MenuItem] opens a small EditorWindow with a file picker for
// content_bundle.json, a folder picker for the output directory, an opt-in auto-reimport toggle, and
// an Import button. The button calls the single tested ContentBundleImporter and shows the
// structured report (never a raw exception).

using UnityEditor;
using UnityEngine;

namespace OWCopilot.ContentImport.Editor
{
    public sealed class ContentBundleImportWindow : EditorWindow
    {
        private string _bundlePath;
        private string _outputDir;
        private ImportReport _lastReport;
        private Vector2 _scroll;

        [MenuItem("Tools/OWCopilot/Import Content Bundle")]
        public static void Open()
        {
            var window = GetWindow<ContentBundleImportWindow>(true, "OWCopilot 内容导入", true);
            window.minSize = new Vector2(460, 320);
            window.Show();
        }

        private void OnEnable()
        {
            _bundlePath = ContentImportSettings.LastBundlePath;
            _outputDir = ContentImportSettings.OutputDir;
        }

        private void OnGUI()
        {
            EditorGUILayout.LabelField("把 OWCopilot 导出的 content_bundle.json 导入为原生 ScriptableObject 资产。",
                EditorStyles.wordWrappedLabel);
            EditorGUILayout.Space();

            using (new EditorGUILayout.HorizontalScope())
            {
                EditorGUILayout.PrefixLabel("内容包 (content_bundle.json)");
                _bundlePath = EditorGUILayout.TextField(_bundlePath);
                if (GUILayout.Button("浏览…", GUILayout.Width(70)))
                {
                    string picked = EditorUtility.OpenFilePanel("选择 content_bundle.json", "", "json");
                    if (!string.IsNullOrEmpty(picked))
                    {
                        _bundlePath = picked;
                    }
                }
            }

            using (new EditorGUILayout.HorizontalScope())
            {
                EditorGUILayout.PrefixLabel("输出目录 (Assets 内)");
                _outputDir = EditorGUILayout.TextField(_outputDir);
                if (GUILayout.Button("浏览…", GUILayout.Width(70)))
                {
                    string picked = EditorUtility.OpenFolderPanel("选择输出目录", Application.dataPath, "");
                    if (!string.IsNullOrEmpty(picked))
                    {
                        _outputDir = ToProjectRelative(picked);
                    }
                }
            }

            bool auto = EditorGUILayout.ToggleLeft(
                "content_bundle.json 在工程内改动时自动重导（默认关闭，人工把关）",
                ContentImportSettings.AutoReimport);
            if (auto != ContentImportSettings.AutoReimport)
            {
                ContentImportSettings.AutoReimport = auto;
            }

            bool scan = EditorGUILayout.ToggleLeft(
                "导入后扫描孤儿资产（源里已删除、库里还在的资产；默认关闭）",
                ContentImportSettings.OrphanScan);
            if (scan != ContentImportSettings.OrphanScan)
            {
                ContentImportSettings.OrphanScan = scan;
            }

            using (new EditorGUI.DisabledScope(!ContentImportSettings.OrphanScan))
            {
                EditorGUI.indentLevel++;
                bool del = EditorGUILayout.ToggleLeft(
                    "并在弹窗确认后删除孤儿资产（默认只报告，永不静默删）",
                    ContentImportSettings.OrphanDelete);
                if (del != ContentImportSettings.OrphanDelete)
                {
                    ContentImportSettings.OrphanDelete = del;
                }
                EditorGUI.indentLevel--;
            }

            EditorGUILayout.Space();
            using (new EditorGUI.DisabledScope(string.IsNullOrEmpty(_bundlePath)))
            {
                if (GUILayout.Button("导入", GUILayout.Height(30)))
                {
                    RunImport();
                }
            }

            if (_lastReport != null)
            {
                EditorGUILayout.Space();
                MessageType type = _lastReport.Ok
                    ? (_lastReport.Warnings.Count > 0 ? MessageType.Warning : MessageType.Info)
                    : MessageType.Error;
                EditorGUILayout.HelpBox(_lastReport.Summary(), type);
                if (_lastReport.Warnings.Count > 0)
                {
                    _scroll = EditorGUILayout.BeginScrollView(_scroll, GUILayout.MaxHeight(160));
                    foreach (string warning in _lastReport.Warnings)
                    {
                        EditorGUILayout.LabelField("• " + warning, EditorStyles.wordWrappedMiniLabel);
                    }
                    EditorGUILayout.EndScrollView();
                }
                if (_lastReport.Orphans.Count > 0)
                {
                    string orphanTitle = _lastReport.OrphansDeleted > 0
                        ? $"孤儿资产（{_lastReport.Orphans.Count} 个，已删 {_lastReport.OrphansDeleted}）"
                        : $"孤儿资产（{_lastReport.Orphans.Count} 个，仅报告）";
                    EditorGUILayout.LabelField(orphanTitle, EditorStyles.boldLabel);
                    foreach (string orphan in _lastReport.Orphans)
                    {
                        EditorGUILayout.LabelField("• " + orphan, EditorStyles.wordWrappedMiniLabel);
                    }
                }
            }
        }

        private void RunImport()
        {
            ContentImportSettings.OutputDir = _outputDir;
            ContentImportSettings.LastBundlePath = _bundlePath;
            _lastReport = ContentBundleImporter.ImportFromFile(_bundlePath, _outputDir);
            Debug.Log("[OWCopilot] " + _lastReport.Summary());
        }

        private static string ToProjectRelative(string absolute)
        {
            absolute = absolute.Replace('\\', '/');
            string dataPath = Application.dataPath.Replace('\\', '/'); // <project>/Assets
            if (absolute == dataPath)
            {
                return "Assets";
            }
            if (absolute.StartsWith(dataPath + "/", System.StringComparison.Ordinal))
            {
                return "Assets" + absolute.Substring(dataPath.Length);
            }
            return absolute; // outside Assets/ — importer will fall back to OS directory creation
        }
    }
}
