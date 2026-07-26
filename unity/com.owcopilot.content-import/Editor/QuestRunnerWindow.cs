// 中心 demo：编辑器内选一个导入得到的 QuestSO，用 Core 的 QuestRunner 真跑其状态机，展示轨迹与最终状态。
// The central demo surface: pick an imported QuestSO and REALLY run its native logic state machine
// through the engine-free Core QuestRunner (via SoToRunner), showing the resulting status / path /
// per-step trace / final variable state — the C# side of "consume QuestLogicSO and run it".
//
// ⚠ REQUIRES REAL UNITY — UNVERIFIED OFFLINE. This EditorWindow (UnityEditor/IMGUI, ScriptableObject
// asset selection) cannot be compiled or exercised on a machine without a Unity editor. Only the
// Core runner it drives is offline-verified (unity/offengine-tests via `dotnet test`, and the
// tests/test_unity_runner_fixtures.py Python golden gate). No offline claim is made about this window.

using System.Collections.Generic;
using System.Text;
using OWCopilot.ContentImport.Core;
using UnityEditor;
using UnityEngine;

namespace OWCopilot.ContentImport.Editor
{
    public sealed class QuestRunnerWindow : EditorWindow
    {
        private QuestSO _quest;
        private string _choicesCsv = "";
        private SimRun _lastRun;
        private Vector2 _scroll;

        private DialogueTreeSO _tree;
        private string _treeChoicesCsv = "";
        private DialogueWalk _lastWalk;
        private Vector2 _treeScroll;

        [MenuItem("Tools/OWCopilot/Quest Runner (Playtest)")]
        public static void Open()
        {
            var window = GetWindow<QuestRunnerWindow>(true, "OWCopilot 任务运行器", true);
            window.minSize = new Vector2(480, 360);
            window.Show();
        }

        private void OnGUI()
        {
            EditorGUILayout.LabelField(
                "选择一个已导入的 QuestSO，用原生逻辑状态机真跑一遍（确定性、无模型调用）。",
                EditorStyles.wordWrappedLabel);
            EditorGUILayout.Space();

            _quest = (QuestSO)EditorGUILayout.ObjectField("Quest 资产", _quest, typeof(QuestSO), false);
            _choicesCsv = EditorGUILayout.TextField(
                new GUIContent("分支选择（可选，逗号分隔的 branch id）"), _choicesCsv);

            using (new EditorGUI.DisabledScope(_quest == null))
            {
                if (GUILayout.Button("运行 (Simulate)", GUILayout.Height(28)))
                {
                    RunSimulation();
                }
            }

            if (_lastRun != null)
            {
                EditorGUILayout.Space();
                EditorGUILayout.HelpBox(Summarize(_lastRun), StatusMessageType(_lastRun.Status));
                _scroll = EditorGUILayout.BeginScrollView(_scroll, GUILayout.MaxHeight(220));
                EditorGUILayout.LabelField(Trace(_lastRun), EditorStyles.wordWrappedMiniLabel);
                EditorGUILayout.EndScrollView();
            }

            EditorGUILayout.Space();
            EditorGUILayout.LabelField(
                "或选择一个已导入的 DialogueTreeSO，用同一套 Core 逻辑真走一遍对白树。",
                EditorStyles.wordWrappedLabel);
            _tree = (DialogueTreeSO)EditorGUILayout.ObjectField(
                "对白树资产", _tree, typeof(DialogueTreeSO), false);
            _treeChoicesCsv = EditorGUILayout.TextField(
                new GUIContent("选择（可选，逗号分隔的 <节点id>:<序号>）"), _treeChoicesCsv);

            using (new EditorGUI.DisabledScope(_tree == null))
            {
                if (GUILayout.Button("行走 (Walk)", GUILayout.Height(28)))
                {
                    RunWalk();
                }
            }

            if (_lastWalk != null)
            {
                EditorGUILayout.Space();
                EditorGUILayout.HelpBox(SummarizeWalk(_lastWalk), WalkStatusMessageType(_lastWalk.Status));
                _treeScroll = EditorGUILayout.BeginScrollView(_treeScroll, GUILayout.MaxHeight(220));
                EditorGUILayout.LabelField(TraceWalk(_lastWalk), EditorStyles.wordWrappedMiniLabel);
                EditorGUILayout.EndScrollView();
            }
        }

        private void RunSimulation()
        {
            RunnerQuest runner = SoToRunner.FromQuest(_quest);
            _lastRun = QuestRunner.Simulate(runner, ParseChoices(_choicesCsv));
        }

        private void RunWalk()
        {
            RunnerDialogueTree runner = SoToRunner.FromTree(_tree);
            _lastWalk = DialogueWalker.Walk(runner, ParseChoices(_treeChoicesCsv));
        }

        private static List<string> ParseChoices(string csv)
        {
            var choices = new List<string>();
            if (string.IsNullOrWhiteSpace(csv))
            {
                return choices;
            }
            foreach (string part in csv.Split(','))
            {
                string trimmed = part.Trim();
                if (trimmed.Length > 0)
                {
                    choices.Add(trimmed);
                }
            }
            return choices;
        }

        private static string Summarize(SimRun run)
        {
            return "结果：" + run.Status
                + "  ·  阶段路径 " + run.Path.Count
                + "  ·  最终变量 " + run.FinalState.Count
                + (string.IsNullOrEmpty(run.Message) ? "" : "  ·  " + run.Message);
        }

        private static MessageType StatusMessageType(string status)
        {
            if (status == "completed")
            {
                return MessageType.Info;
            }
            if (status == "blocked" || status == "dangling")
            {
                return MessageType.Error;
            }
            return MessageType.Warning; // deadlock / cycle / unavailable (quest-level gate not met)
        }

        private static string Trace(SimRun run)
        {
            var sb = new StringBuilder();
            sb.Append("path: ").Append(string.Join(" -> ", run.Path)).Append('\n');
            foreach (SimStep step in run.Steps)
            {
                sb.Append("• ").Append(step.StageId)
                    .Append(step.PreconditionOk ? "" : " [precondition false]");
                if (!string.IsNullOrEmpty(step.BranchTaken))
                {
                    sb.Append("  branch=").Append(step.BranchTaken);
                }
                foreach (string effect in step.Effects)
                {
                    sb.Append("  {").Append(effect).Append('}');
                }
                sb.Append('\n');
            }
            sb.Append("final_state:\n");
            foreach (KeyValuePair<string, RunnerValue> kv in run.FinalState)
            {
                sb.Append("    ").Append(kv.Key).Append(" = ").Append(kv.Value.ToWireString()).Append('\n');
            }
            return sb.ToString();
        }

        // --- dialogue-walk rendering (mirrors the quest-sim helpers above) --------------------------

        private static string SummarizeWalk(DialogueWalk walk)
        {
            return "结果：" + walk.Status
                + "  ·  节点路径 " + walk.Path.Count
                + "  ·  最终变量 " + walk.FinalState.Count
                + (string.IsNullOrEmpty(walk.Message) ? "" : "  ·  " + walk.Message);
        }

        private static MessageType WalkStatusMessageType(string status)
        {
            if (status == "completed")
            {
                return MessageType.Info;
            }
            if (status == "dangling")
            {
                return MessageType.Error;
            }
            return MessageType.Warning; // deadlock / cycle
        }

        private static string TraceWalk(DialogueWalk walk)
        {
            var sb = new StringBuilder();
            sb.Append("path: ").Append(string.Join(" -> ", walk.Path)).Append('\n');
            foreach (DialogueWalkStep step in walk.Steps)
            {
                sb.Append("• ").Append(step.NodeId);
                if (!string.IsNullOrEmpty(step.Chosen))
                {
                    sb.Append("  chosen=").Append(step.Chosen);
                }
                foreach (string effect in step.Effects)
                {
                    sb.Append("  {").Append(effect).Append('}');
                }
                sb.Append('\n');
            }
            sb.Append("final_state:\n");
            foreach (KeyValuePair<string, RunnerValue> kv in walk.FinalState)
            {
                sb.Append("    ").Append(kv.Key).Append(" = ").Append(kv.Value.ToWireString()).Append('\n');
            }
            return sb.ToString();
        }
    }
}
