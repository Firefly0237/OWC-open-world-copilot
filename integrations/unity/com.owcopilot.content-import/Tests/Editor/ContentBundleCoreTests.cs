// C# 侧契约测试：解析真实导出的 content_bundle.json，跑两遍解析核心，交叉断言与 Python 侧一致的
// 不变量（实体/任务/对白/关系计数、已解析/待复核引用、逐字告警模板）。核心是 UnityEngine-free，
// 因此这些 [Test] 既能在 Unity EditMode 运行，也能被 `dotnet test` 在无引擎机器上运行。
// The fixture is the SAME bytes the Python exporter emits (locked by tests/test_unity_package.py),
// so both sides assert one contract, not two copies.

using System.IO;
using System.Linq;
using NUnit.Framework;
using OWCopilot.ContentImport.Core;

namespace OWCopilot.ContentImport.Tests
{
    public sealed class ContentBundleCoreTests
    {
        // Package-relative path; works under a UPM install (Packages/<name>/...) and an embedded copy.
        private const string FixturePath =
            "Packages/com.owcopilot.content-import/Tests/Editor/Fixtures/content_bundle.json";

        private static ContentBundleDto LoadFixture()
        {
            string path = FixturePath;
            if (!File.Exists(path))
            {
                // Fallback for off-engine `dotnet test`, where cwd is the package/Tests folder.
                path = Path.Combine(
                    TestContext.CurrentContext.TestDirectory, "Fixtures", "content_bundle.json");
            }
            Assert.IsTrue(File.Exists(path), "fixture not found at " + path);
            return ContentBundleParser.Parse(File.ReadAllText(path));
        }

        [Test]
        public void Parse_ReadsDictKeyedCollections()
        {
            ContentBundleDto bundle = LoadFixture();
            Assert.AreEqual(3, bundle.Entities.Count);
            Assert.AreEqual(2, bundle.Quests.Count);
            Assert.AreEqual(1, bundle.Dialogues.Count);
            Assert.AreEqual(3, bundle.Relations.Count);
        }

        [Test]
        public void Parse_ReadsQuestLogicUnionValues()
        {
            ContentBundleDto bundle = LoadFixture();
            QuestLogicDto logic = bundle.Quests["quest_patrol"].Logic;
            Assert.IsNotNull(logic);
            Assert.AreEqual("v_seen", logic.Variables[0].Id);
            // frozen bool|int|str union: the effect value deserializes to a boxed bool
            Assert.AreEqual(true, logic.StageLogic[0].EffectsOnComplete[0].Value);
            Assert.AreEqual("quest_next", logic.Unlocks[0]);
        }

        [Test]
        public void Resolve_ReportsResolvedAndUnresolvedReferences()
        {
            ResolveResult result = ContentBundleResolver.Resolve(LoadFixture());

            // Four references cannot resolve and are kept for review, never fabricated:
            // quest_missing (prerequisite), dlg_ghost (dialogue_ref), ent_ghost (stage entity),
            // ghost_x (relation source).
            Assert.AreEqual(4, result.UnresolvedLinkCount);
            Assert.AreEqual(4, result.Warnings.Count);
            Assert.IsTrue(result.ResolvedLinkCount > result.UnresolvedLinkCount);
        }

        [Test]
        public void Resolve_UsesTheSameChineseWarningTemplateAsEngineData()
        {
            ResolveResult result = ContentBundleResolver.Resolve(LoadFixture());
            // Byte-for-byte the recognize/engine_data.py template.
            Assert.Contains("行「quest_patrol」引用未知对象「quest_missing」，保留待人审复核", result.Warnings);
            Assert.Contains("行「ghost_x」引用未知对象「ghost_x」，保留待人审复核", result.Warnings);
        }

        [Test]
        public void Resolve_KnownReferencesAreMarkedResolved()
        {
            ResolveResult result = ContentBundleResolver.Resolve(LoadFixture());
            ReferenceLink giver = result.Links.Single(
                l => l.OwnerId == "quest_patrol" && l.Field == "giver_npc");
            Assert.IsTrue(giver.Resolved);
            Assert.AreEqual("npc_mara", giver.TargetId);
            Assert.AreEqual(ContentKind.Entity, giver.TargetKind);
        }

        [Test]
        public void Parse_EmptyJson_RaisesGuidedError()
        {
            var ex = Assert.Throws<ContentBundleParseException>(() => ContentBundleParser.Parse(""));
            Assert.IsTrue(ex.Message.Contains("content_bundle.json"));
        }
    }
}
