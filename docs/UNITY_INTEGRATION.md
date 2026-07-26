# Unity 集成运行手册（com.owcopilot.content-import）

> 把 OWCopilot 导出的 `content_bundle.json` 真正落进 Unity 工程的原生 ScriptableObject 资产。
> 纯文本 UPM 包、编辑器内、版本无关、无 vendored 二进制。
> **现状：真机/工具链联调待办（live run pending）。** 离线可验证的部分已由 CI 测试锁死（见文末清单）；
> 只有装了 Unity 或 dotnet SDK 的机器才能证实的环节在末尾单列，不谎称已绿。

## 组成

包路径 `unity/com.owcopilot.content-import/`（可拷进消费工程 `Packages/`，或经 git URL `?path=` 安装）：

| 目录 | 程序集 | 进游戏包 | 职责 |
| --- | --- | --- | --- |
| `Runtime/` | `com.owcopilot.content-import` | 是 | ScriptableObject 数据类型 |
| `Core/` | `…​.Core`（`noEngineReferences: true`） | 是 | Newtonsoft 解析 + 两遍解析（UnityEngine-free，可离线 `dotnet test`） |
| `Editor/` | `…​.Editor`（Editor-only） | 否 | 导入器、菜单/窗口、AssetPostprocessor |
| `Tests/Editor/` | `…​.Editor.Tests` | 否 | NUnit 契约测试，跑在“与 Python 导出器逐字节一致”的 fixture 上 |

Newtonsoft 走官方 `com.unity.nuget.newtonsoft-json` 依赖声明（`package.json`），**绝不 vendor DLL**。

## 数据模型映射（DTO → ScriptableObject）

`content_bundle.json` 是**字典键控**（`entities/quests/dialogues` 是 `{id: obj}`）且**嵌套**的，
`JsonUtility` 既不能反序列化 `Dictionary` 也不能反序列化嵌套多态字段，因此用 Newtonsoft 解析成
`Core/Dtos.cs` 的 DTO，再由 `Editor/ContentBundleImporter.cs` 显式映射到 SO：

- `EntitySO`：`Id/DisplayName/Type/Description/Aliases/Tags/Status` + 溯源（`Origin/ReviewStatus`）。
- `QuestSO`：`Title/Objective/TimelineOrder`，引用字段各存**原始 id + 已解析对象引用**两份
  （`GiverNpcId`/`GiverNpc`、`LocationId`/`Location`、`PrerequisiteIds`/`Prerequisites`、
  `DialogueRefIds`/`DialogueRefs`），`Stages`，以及原生逻辑层 `QuestLogic`
  （`Variables/Precondition/StageLogic/Branches/Unlocks`；`LogicVar.default`、`Effect.value` 的
  冻结 `bool|int|str` 联合用可序列化的 `LogicValue{Kind,BoolValue,IntValue,StringValue}` 忠实表示）。
- `DialogueSO`：`TextKey/SpeakerId(+Speaker)/QuestId(+Quest)/Text/Locale`。
- `DialogueTreeSO`：分支会话图。`QuestId(+Quest)/RootNodeId/Nodes`；节点 `Id/SpeakerId(+Speaker)/
  Text/Choices/NextNodeId(+NextNode)`，选项 `Text/NextNodeId(+NextNode)/Condition/Effects`（`Effects`
  复用与任务逻辑同一份冻结 `Effect`）。节点存在**同一资产内**（非独立 `.asset`），用
  `[SerializeReference]` 序列化，使选项/next 能指回**同一个**节点实例并允许环。原始 id 恒为权威，
  已解析对象引用是编辑器导航便利——`SoToRunner.FromTree` 只按原始 id 重建运行器树喂给 `DialogueWalker`
  （与离线 `RunnerModelFactory.TreeFromDto` 同形），所以受测的运行器路径不依赖托管引用如何序列化。
- `RelationSO`：`SourceId(+Source)/TargetId(+Target)/Kind`；关系无自带 id，导入器按
  `rel_{source}_{kind}_{target}` 生成确定性资产名。
- `ContentRegistrySO`：`id → 资产` 的可序列化列表（Unity 不能序列化 `Dictionary`）+ 关系列表。

线格式枚举是小写 snake_case（`npc`/`ai_draft`/`pending_review`/`bool`/`set`），映射时下划线无关地
解析到 C# 命名；未知值回退到安全默认（不抛异常）。

## 两遍导入契约

- **第 1 遍**：对每个 entity/quest/dialogue/dialogue_tree 建/覆盖一个 `.asset`，构建 `id → SO` 映射
  （对白树的节点/选项图连同原始 id 一并建好）。
- **第 2 遍**：按 `Core/ContentBundleResolver` 的解析结果，把字符串 id 引用接成真实对象引用；对白树的
  每节点 speaker、owning-quest 及**树内**选项/next 节点引用在此接好（树内链接按树自身的节点 id 图解析）。
- **未解析引用**：id 在对应集合里查不到时，**保留原始 id 字段 + 记一条告警**，对象引用留空，
  绝不伪造目标——与 `recognize/engine_data.py` 同一纪律，告警模板逐字一致：
  `行「{source}」引用未知对象「{target}」，保留待人审复核`。
- **GUID 稳定（幂等重导）**：重导时**不 delete+recreate**（那会换新 GUID、打断所有场景/预制体引用），
  而是 `LoadAssetAtPath` 取到既有资产、**就地覆盖字段** + `SetDirty`，最后一次性
  `SaveAssets`+`Refresh`。相同 id 跨多次导入映射到同一资产，引用稳定。

## 三种导入路径（同一份被测代码）

1. **菜单**：`Tools > OWCopilot > Import Content Bundle` 开窗口，选 `content_bundle.json`、选
   `Assets/` 内输出目录、点导入；结构化结果就地显示（不弹裸异常）。
2. **AssetPostprocessor 自动重导（opt-in）**：工程内 `content_bundle.json` 变化时自动重跑导入。
   **默认关闭**（引擎导入保持人工把关，与 `engine_sync` 一致）；回调必为 `static`，且因导入会写
   `.asset` 触发回调，内置**重入守卫**防止无限重导（官方文档明确的必需项）。
3. 任意 MCP / 脚本可直接调用 `ContentBundleImporter.ImportFromFile(...)`——正确性只在一处被测。

## 孤儿清理（opt-in，两档，绝不静默删）

导入后可选地比对「本次 bundle 的 id 集」与「磁盘上工具输出子目录里既有资产的 id 集」，把源里已删除的
孤儿资产找出来。两个开关都**默认关闭**（与自动重导同一人工把关纪律）：

- **孤儿扫描**：开启后把孤儿清单写进导入报告（`ImportReport.Orphans`），仅报告不动资产。
- **孤儿删除**（须先开扫描）：额外提供删除，但**绝不静默**——每次删除前弹 `EditorUtility.DisplayDialog`
  确认。扫描严格限定在工具自己的输出子目录（`Entities/Quests/Dialogues/DialogueTrees/Relations`），
  设计师在别处手建的同类资产**根本不会被发现**（位置即溯源信号；`ContentObjectSO` 无「本工具生成」标记，
  故以目录归属为准）。

## Unity Localization 字符串表 CSV（opt-in，官方 CSV 扩展）

内容导入器把 `LocalizedText` 落成 `DialogueSO.Text/Locale` 等字段；若团队用 Unity 官方 Localization
包（`com.unity.localization`）管理译文，可再 opt-in 导出一份 **String Table Collection CSV**，经该包的
CSV 扩展直接导入。

- **开关**：CLI `owc export --target-engine unity --unity-localization`；service `POST
  /projects/{project}/exports` 请求体 `{"target_engine": "unity", "unity_localization": true}`。
  **默认关**——不开时 `unity/` 产物逐字节与从前一致。
- **产物**：`unity/localization/owcopilot_strings.csv`（单张表，全量 `LocalizedText`）+ 同目录
  `README.md`。`unity/unity_import_manifest.json` 同时长出一个 `localization` 交叉核对块
  （collection 名 / CSV 相对路径 / locales / string_count）。
- **格式**（严格按官方 CSV 扩展，不自创列名）：首列 `Key`（=`text_key`），随后每个 locale 一列，
  列头为 `LocaleIdentifier.ToString()` 形状 `<Locale 名称>(<locale-code>)`（如 `English(en)`、
  `Chinese (Simplified)(zh-CN)`，括号前无空格）。官方文档：「CSV data must contain a Key or Id
  column. All other columns are optional.」——故只发文档确定无疑的最小子集（Key + locale 列），
  不发 `Id`/`Shared Comments`。**无某 locale 译文的格子留空**，绝不用默认语言回填（那会伪造翻译状态）。
- **导入路径**：Window ▸ Asset Management ▸ Localization Tables 打开表窗口 → 右上角竖排省略号（⋮）
  ▸ Import ▸ 选 `owcopilot_strings.csv`。`Key` 列匹配条目键，`<名称>(<code>)` 列按 locale 匹配。
- **列头自动匹配的已知边界**：括号内的 `(code)` 是 Unity 匹配所依赖、稳定可靠的机器部分；前缀显示名
  仅驱动导入时的默认列自动映射。地区限定码（如 `zh-CN`）的真实 `CultureInfo.EnglishName` 随 .NET/ICU
  运行时而异，若团队 Locale 资产名与本文件列头显示名不一致，在导入对话框里对该列改一次映射即可
  （`(code)` 仍能标识它）。未收录显示名的 locale 码退化为「裸 code」列头。
- **为什么走 CSV 而非在 UPM 包里写 Localization 代码**：CSV 是官方**解耦**通道。若在
  `com.owcopilot.content-import` 包里直接依赖 Localization，会给包引入 `com.unity.localization`
  **硬依赖**，违反导入器包的最小依赖原则；CSV 让导入器包保持零 Localization 依赖，用 Localization 的
  团队一键导入即可——这正是选它的原因。

## 仅真机 / 工具链可验证清单（live run pending，未声称已绿）

以下环节离线无 Unity / 无 dotnet SDK 无法证实，须在装了对应工具链的机器上核对：

1. **C# 编译**：本仓库当前环境有 dotnet 运行时但**无 SDK**（`C:/Program Files/dotnet/sdk` 缺失），
   故 `dotnet build`/`dotnet test` 无法在此运行。Core 程序集刻意 `noEngineReferences`，装了
   .NET 8 SDK 后一条命令即可跑正式的离线工程（见下节 §off-engine）：
   `dotnet test unity/offengine-tests/OWCopilot.ContentImport.OffEngine.Tests.csproj`。
2. **EditMode 全链路**：真正写出 `.asset`、GUID 跨重导稳定、`AssetPostprocessor` 触发、`Dictionary`
   不可序列化的回退是否成立——需授权 Editor 无头跑：
   `Unity -batchmode -nographics -runTests -testPlatform editmode -testResults results.xml`。
   本轮新增的 `DialogueTreeSO`（`[SerializeReference]` 节点图的存/取与环）、`SoToRunner.FromTree` 桥、
   孤儿扫描/删除均属 Editor/Runtime 面，`offengine-tests` 只测 Core（DialogueWalker/TreeFromDto），
   故这些新件离线无法编译或测试，只做了逐字核对 + 与既有代码同构。
3. **菜单 / 文件选择器**渲染与交互（`EditorUtility.OpenFilePanel/OpenFolderPanel`）。
4. **Newtonsoft 依赖解析**：Package Manager 是否按 `package.json` 正确拉取
   `com.unity.nuget.newtonsoft-json@3.2.1`，`Unity.Newtonsoft.Json` 程序集名是否被各 asmdef 正确引用。
5. **.meta GUID 稳定性**：首次在真实工程导入后 Unity 是否保留我们写入的 GUID（本包已随附 `.meta`，
   正是为了不让消费工程重新 mint GUID）。

## off-engine：QuestRunner 跨语言一致性（E4）

包内 `Core/` 现含一个**任务状态机运行时**：`QuestLogicExpr.cs`（表达式 DSL 的词法+递归下降解析，
逐规则移植 `logic/expr.py`）、`QuestLogicEval.cs`（严格类型求值，移植 `semantics.py`，含 Python 式
truthiness）、`QuestRunner.cs`（单路径模拟，移植 `simulate.py`：阶段前置/阶段+分支效果次序/
队列优先分支选择/四种终态/阶段空间 cycle）、`DialogueWalker.cs`（对白行走，移植
`logic/dialogue.py`）。Editor 侧配 `QuestRunnerWindow.cs`（当前阶段/可选分支/变量面板演示壳,
真机项见清单）与 `SoToRunner.cs`（SO→运行时模型）。

**验收方式=跨语言差分测试**（生产 Python 当参考 oracle）：
- 夹具 `Tests/Editor/Fixtures/runner_bundle.json` + `runner_traces.json` 由
  `scripts/gen_runner_traces.py` 从生产 `simulate_quest`/`walk_dialogue_tree` 生成,
  Python 侧 `tests/test_unity_runner_fixtures.py` 做**字节门**（重生成必须逐字节相等）、
  互证与状态覆盖检查（4 种模拟状态+4 种对白状态+对白闭环场景全在）；
- C# 侧 `Tests/Editor/QuestRunnerCrossLangTests.cs` 读同一夹具复算,逐字段断言
  status/path/steps/final_state（message 因语言差异排除）；
- 离线工程 `unity/offengine-tests/OWCopilot.ContentImport.OffEngine.Tests.csproj`
  （装 .NET 8 SDK 后 `dotnet test` 一条命令,无需 Unity）。
- 已知的两处**刻意分叉**（带 ⚠ 注释）：超出 Int64 的整数字面量与全角数字——Python 任意精度可算,
  C# 判为语法错误并安全降级为条件不满足（不崩溃）；审计层在运行前就会拒绝此类字面量。

## 离线已锁（CI 契约门，`tests/test_unity_package.py`）

- **清单/程序集形状**：`package.json`（reverse-DNS 名、SemVer、`unity` 版本、Newtonsoft 精确依赖）；
  Runtime/Core/Editor/Tests 四程序集拆分（Core `noEngineReferences`、Editor/Tests `Editor` 平台、
  Tests 挂 `nunit.framework.dll` + `UNITY_INCLUDE_TESTS`）。
- **.meta / GUID 纪律**：每个资产与子文件夹都随附 `.meta`，GUID 为合法 32 位十六进制且全局唯一；
  隐藏目录（`Documentation~`）不带也不需 `.meta`。
- **fixture 逐字节忠实**：`Tests/Editor/Fixtures/content_bundle.json` 与**真实** `export_content_bundle`
  的输出逐字节相等——这保证 C# 测试跑在真线格式上，也把两侧共享的契约做成字节回归门。
- **无二进制**：包内无 `.dll`/`.uasset`，无 `Library/Temp/obj/Logs` 工程脚手架。
