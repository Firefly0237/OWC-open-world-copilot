// C# 侧跨语言差分测试：以生产 simulate_quest/walk_dialogue_tree 的输出为参考预言机（golden
// runner_traces.json），用同一份 runner_bundle.json 喂进 Core 的 QuestRunner/DialogueWalker，逐场景
// 断言 status/path/steps/final_state 一致（message 排除：中文 vs 任意 C# 文本）。核心 UnityEngine-free，
// 故这些 [Test] 既能在 Unity EditMode 运行，也能被 integrations/unity/offengine-tests 的 `dotnet test` 离线运行。
// Differential testing (reference-oracle method): production simulate_quest/walk_dialogue_tree is the
// oracle, this drives the SECOND implementation (Core QuestRunner/DialogueWalker) over the SAME
// shared corpus, and agreement on status/path/steps/final_state is the gate. Every scenario stores
// its choices + initial_state so the replay is a FIXED script — no explore/oracle logic here.

using System.Collections.Generic;
using System.IO;
using Newtonsoft.Json.Linq;
using NUnit.Framework;
using OWCopilot.ContentImport.Core;

namespace OWCopilot.ContentImport.Tests
{
    public sealed class QuestRunnerCrossLangTests
    {
        private const string BundlePackagePath =
            "Packages/com.owcopilot.content-import/Tests/Editor/Fixtures/runner_bundle.json";
        private const string TracesPackagePath =
            "Packages/com.owcopilot.content-import/Tests/Editor/Fixtures/runner_traces.json";

        private static string Resolve(string packagePath, string fileName)
        {
            if (File.Exists(packagePath))
            {
                return packagePath;
            }
            // Fallback for off-engine `dotnet test`, where the fixtures are copied next to the tests.
            return Path.Combine(TestContext.CurrentContext.TestDirectory, "Fixtures", fileName);
        }

        private static ContentBundleDto LoadBundle()
        {
            string path = Resolve(BundlePackagePath, "runner_bundle.json");
            Assert.IsTrue(File.Exists(path), "runner_bundle.json not found at " + path);
            return ContentBundleParser.Parse(File.ReadAllText(path));
        }

        private static JObject LoadTraces()
        {
            string path = Resolve(TracesPackagePath, "runner_traces.json");
            Assert.IsTrue(File.Exists(path), "runner_traces.json not found at " + path);
            return JObject.Parse(File.ReadAllText(path));
        }

        [Test]
        public void QuestScenarios_MatchProductionOracle()
        {
            ContentBundleDto bundle = LoadBundle();
            JObject traces = LoadTraces();
            var scenarios = (JArray)traces["quest_scenarios"];
            Assert.IsNotNull(scenarios, "quest_scenarios missing");
            Assert.Greater(scenarios.Count, 0, "no quest scenarios");

            foreach (JToken token in scenarios)
            {
                var scenario = (JObject)token;
                string name = scenario["name"].Value<string>();
                string questId = scenario["quest_id"].Value<string>();
                Assert.IsTrue(bundle.Quests.ContainsKey(questId), "bundle missing quest " + questId);

                RunnerQuest quest = RunnerModelFactory.QuestFromDto(bundle.Quests[questId]);
                SimRun run = QuestRunner.Simulate(
                    quest, StringList(scenario["choices"]), State(scenario["initial_state"]));
                AssertSimMatches(run, (JObject)scenario["expected"], "quest[" + name + "]");
            }
        }

        [Test]
        public void DialogueScenarios_MatchProductionOracle()
        {
            ContentBundleDto bundle = LoadBundle();
            JObject traces = LoadTraces();
            var scenarios = (JArray)traces["dialogue_scenarios"];
            Assert.IsNotNull(scenarios, "dialogue_scenarios missing");
            Assert.Greater(scenarios.Count, 0, "no dialogue scenarios");

            foreach (JToken token in scenarios)
            {
                var scenario = (JObject)token;
                string name = scenario["name"].Value<string>();
                string treeId = scenario["tree_id"].Value<string>();
                Assert.IsTrue(bundle.DialogueTrees.ContainsKey(treeId), "bundle missing tree " + treeId);

                RunnerDialogueTree tree = RunnerModelFactory.TreeFromDto(bundle.DialogueTrees[treeId]);
                DialogueWalk walk = DialogueWalker.Walk(
                    tree, StringList(scenario["choices"]), State(scenario["initial_state"]));
                AssertWalkMatches(walk, (JObject)scenario["expected"], "dialogue[" + name + "]");
            }
        }

        [Test]
        public void ClosureScenarios_DialogueFinalStateFlowsIntoQuest()
        {
            ContentBundleDto bundle = LoadBundle();
            JObject traces = LoadTraces();
            var scenarios = (JArray)traces["closure_scenarios"];
            Assert.IsNotNull(scenarios, "closure_scenarios missing");
            Assert.Greater(scenarios.Count, 0, "no closure scenarios");

            foreach (JToken token in scenarios)
            {
                var scenario = (JObject)token;
                string name = scenario["name"].Value<string>();

                RunnerDialogueTree tree =
                    RunnerModelFactory.TreeFromDto(bundle.DialogueTrees[scenario["tree_id"].Value<string>()]);
                DialogueWalk walk = DialogueWalker.Walk(
                    tree,
                    StringList(scenario["dialogue_choices"]),
                    State(scenario["dialogue_initial_state"]));
                AssertWalkMatches(walk, (JObject)scenario["dialogue_expected"], "closure[" + name + "].dialogue");

                // The closure contract: the dialogue walk's final_state becomes the quest's initial.
                RunnerQuest quest =
                    RunnerModelFactory.QuestFromDto(bundle.Quests[scenario["quest_id"].Value<string>()]);
                SimRun run = QuestRunner.Simulate(
                    quest, StringList(scenario["quest_choices"]), walk.FinalState);
                AssertSimMatches(run, (JObject)scenario["quest_expected"], "closure[" + name + "].quest");
            }
        }

        [Test]
        public void Corpus_CoversEverySimAndWalkStatus()
        {
            JObject traces = LoadTraces();
            var simStatuses = new HashSet<string>();
            bool terminalOutcome = false;
            foreach (JToken token in (JArray)traces["quest_scenarios"])
            {
                var expected = (JObject)token["expected"];
                simStatuses.Add(expected["status"].Value<string>());
                var steps = (JArray)expected["steps"];
                if (steps.Count > 0
                    && expected["status"].Value<string>() == "completed"
                    && !string.IsNullOrEmpty(((JObject)steps[steps.Count - 1])["branch_taken"].Value<string>()))
                {
                    terminalOutcome = true; // completed via a terminal branch (empty to_stage)
                }
            }
            foreach (string status in new[] { "completed", "deadlock", "blocked", "cycle", "unavailable" })
            {
                Assert.IsTrue(simStatuses.Contains(status), "sim status not covered: " + status);
            }
            Assert.IsTrue(terminalOutcome, "no terminal-branch outcome scenario");

            var walkStatuses = new HashSet<string>();
            foreach (JToken token in (JArray)traces["dialogue_scenarios"])
            {
                walkStatuses.Add(((JObject)token["expected"])["status"].Value<string>());
            }
            foreach (string status in new[] { "completed", "dangling", "deadlock", "cycle" })
            {
                Assert.IsTrue(walkStatuses.Contains(status), "walk status not covered: " + status);
            }

            Assert.Greater(((JArray)traces["closure_scenarios"]).Count, 0, "no dialogue closure");
        }

        // --- comparison helpers (status/path/steps/final_state only; message excluded) -------------

        private static void AssertSimMatches(SimRun run, JObject expected, string ctx)
        {
            Assert.AreEqual(expected["status"].Value<string>(), run.Status, ctx + " status");
            AssertStringList(expected["path"], run.Path, ctx + " path");

            var steps = (JArray)expected["steps"];
            Assert.AreEqual(steps.Count, run.Steps.Count, ctx + " step count");
            for (int i = 0; i < steps.Count; i++)
            {
                var es = (JObject)steps[i];
                SimStep actual = run.Steps[i];
                string where = ctx + " step[" + i + "]";
                Assert.AreEqual(es["stage_id"].Value<string>(), actual.StageId, where + " stage_id");
                Assert.AreEqual(
                    es["precondition_ok"].Value<bool>(), actual.PreconditionOk, where + " precondition_ok");
                Assert.AreEqual(
                    es["branch_taken"].Value<string>(), actual.BranchTaken, where + " branch_taken");
                AssertStringList(es["effects"], actual.Effects, where + " effects");
            }
            AssertStateEquals(run.FinalState, (JObject)expected["final_state"], ctx);
        }

        private static void AssertWalkMatches(DialogueWalk walk, JObject expected, string ctx)
        {
            Assert.AreEqual(expected["status"].Value<string>(), walk.Status, ctx + " status");
            AssertStringList(expected["path"], walk.Path, ctx + " path");

            var steps = (JArray)expected["steps"];
            Assert.AreEqual(steps.Count, walk.Steps.Count, ctx + " step count");
            for (int i = 0; i < steps.Count; i++)
            {
                var es = (JObject)steps[i];
                DialogueWalkStep actual = walk.Steps[i];
                string where = ctx + " step[" + i + "]";
                Assert.AreEqual(es["node_id"].Value<string>(), actual.NodeId, where + " node_id");
                Assert.AreEqual(es["chosen"].Value<string>(), actual.Chosen, where + " chosen");
                AssertStringList(es["effects"], actual.Effects, where + " effects");
            }
            AssertStateEquals(walk.FinalState, (JObject)expected["final_state"], ctx);
        }

        private static void AssertStateEquals(
            IDictionary<string, RunnerValue> actual, JObject expected, string ctx)
        {
            Assert.AreEqual(expected.Count, actual.Count, ctx + " final_state key count");
            foreach (JProperty prop in expected.Properties())
            {
                Assert.IsTrue(
                    actual.TryGetValue(prop.Name, out RunnerValue got),
                    ctx + " final_state missing key " + prop.Name);
                RunnerValue want = ValueFromToken(prop.Value);
                Assert.IsTrue(
                    want.Equals(got),
                    ctx + " final_state[" + prop.Name + "] expected " + want.ToWireString()
                        + " got " + got.ToWireString());
            }
        }

        private static void AssertStringList(JToken expected, IReadOnlyList<string> actual, string ctx)
        {
            var want = (JArray)expected;
            Assert.AreEqual(want.Count, actual.Count, ctx + " length");
            for (int i = 0; i < want.Count; i++)
            {
                Assert.AreEqual(want[i].Value<string>(), actual[i], ctx + "[" + i + "]");
            }
        }

        private static List<string> StringList(JToken token)
        {
            var result = new List<string>();
            foreach (JToken item in (JArray)token)
            {
                result.Add(item.Value<string>());
            }
            return result;
        }

        private static Dictionary<string, RunnerValue> State(JToken token)
        {
            var result = new Dictionary<string, RunnerValue>();
            foreach (JProperty prop in ((JObject)token).Properties())
            {
                result[prop.Name] = ValueFromToken(prop.Value);
            }
            return result;
        }

        private static RunnerValue ValueFromToken(JToken token)
        {
            switch (token.Type)
            {
                case JTokenType.Boolean:
                    return RunnerValue.OfBool(token.Value<bool>());
                case JTokenType.Integer:
                    return RunnerValue.OfInt(token.Value<long>());
                case JTokenType.String:
                    return RunnerValue.OfString(token.Value<string>());
                default:
                    throw new AssertionException("unexpected value token type: " + token.Type);
            }
        }
    }
}
