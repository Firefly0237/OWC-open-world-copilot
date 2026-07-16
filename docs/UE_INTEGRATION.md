# Unreal Engine 集成运行手册（OWCopilotBridge）

> 把 OWCopilot 导出的 DataTable JSON 真正落进 UE 工程的 DataTable 资产。零 C++、编辑器内、版本无关。
> **现状：真机联调待办（live run pending）。** 离线可验证的部分已由 CI 测试锁死（见文末清单）；
> 只有真实 UE 机器才能证实的环节在末尾单列，不谎称已绿。

## 组成

- **内容型插件** `integrations/unreal/OWCopilotBridge/`（随游戏工程发布，不进 Python wheel）
  - `OWCopilotBridge.uplugin` — 描述符：`CanContainContent: true`，无 `Modules`/无 `Source/`，
    依赖 `PythonScriptPlugin`。内容型插件不产出 `Binaries/`、`Intermediate/`（已在 .gitignore 防御性忽略）。
  - `Content/Python/init_unreal.py` — 编辑器启动自动执行，注册 `OWCopilot > Import content bundle
    into DataTable...` 主菜单项。
  - `Content/Python/owcopilot_bridge/` — 唯一被测的导入器（stdlib-only，`import unreal` 有守卫，
    离线可导入）：`import_bundle` / `import_rows` / `plan_bundle_import` / `merge_rows_by_name`。
- **venv 侧远程客户端** `src/owcopilot/integrations/unreal_remote.py` — 两条官方无 C++ 传输的薄壳
  （HTTP 用 stdlib，Remote Execution 运行时可选导入 Epic 的 `remote_execution.py`）。

三条驱动、一套代码：菜单按钮、Python Remote Execution、Remote Control HTTP 最终都调用同一个
`owcopilot_bridge.import_bundle`，正确性只测一处。

## Row Struct 字段规格（消费工程必须自建）

导出时 `exporters/unreal_datatable.py` 会生成 `OWCopilotRows.h` —— 那是**权威**的行结构规格
（每个结构派生 `FTableRowBase`）。在 UE 工程里按它创建 `USTRUCT`，并把各 DataTable 的 Row Struct
指向对应结构：

- `FQuestRow`（Quests.json）：`Type FString`、`DisplayName FText`、`Objective FText`、
  `GiverNPC FDataTableRowHandle`、`Location FDataTableRowHandle`、
  `Prerequisites TArray<FDataTableRowHandle>`、`DialogueRefs TArray<FDataTableRowHandle>`、
  `TimelineOrder int32`。
- `FEntityRow`（Entities.json）：`Type/DisplayName/Description/Status`，外加按关系种类动态追加的
  `TArray<FDataTableRowHandle>` 列（如 `resides_in`）。
- `FDialogueRow`（Dialogues.json）：`Type FString`、`Speaker FDataTableRowHandle`、
  `Quest FDataTableRowHandle`、`Text FText`。

约束（导出器 preflight 已强制）：字段名不得等于保留行键 `Name`、不含空格、须是合法标识符；
`bool` 导出名去掉前导 `b`（`bIsActive` <-> 导出 `IsActive`）。本地化：`text_mode="stringtable"` 时
`Text` 写的是 StringTable key，源串在同目录 `<table>_strings.csv`（Epic `Key,SourceString` 两列）。

### 全保真子表（QuestLogic / 阶段 / 奖励 / 对白树）

上面三表是**有损投影**。任务逻辑（变量 / 前置 / effects / 分支）、阶段、奖励、对白树是嵌套结构，
UE 平面行装不下，故按**配表管线标准做法**拆成子表：每行带一个指回父对象的 `FDataTableRowHandle` 外键
（RowName = `父id::子id` 复合稳定键），图指针（分支 from/to 阶段、节点 / 选项 next_node、树 root_node）
是指向兄弟子表的 `FDataTableRowHandle`——即米哈游 `nextDialogs` 式的指针串图，正是可达性审计要走的图。
两类字段留作字符串列（均属正规）：布尔表达式源串（`Precondition` / `Condition`，任意深度表达式）；
effects 列编码为 `[{"var","op","value"}]` 紧凑 JSON——与 C# `EffectDto` / `content_bundle.json`
同一字段词汇（`Effect.model_dump`），UE 侧解析即得模拟器所用的同一 effect（`Effect` 无 id，做不成有键子表）。

这些子表是**加性**的：原三表行结构与文件名逐字节不变，旧 UE 工程不受影响；仅当 bundle 真有对应数据才写出。

- `FQuestStageRow`（QuestStages.json，`QuestStage` ∪ `StageLogic` 按 stage_id 合并）：
  `Type FString`、`Quest FDataTableRowHandle`、`StageId FString`、`Ordinal int32`、`Summary FText`、
  `Location FDataTableRowHandle`、`RequiredEntities TArray<FDataTableRowHandle>`、
  `Precondition FString`、`EffectsOnComplete FString`(JSON)。
- `FQuestLogicVarRow`（QuestLogicVars.json，`LogicVar`）：`Type`、`Quest`、`VarId FString`、
  `VarName FString`、`VarType FString`、`DefaultValue FString`(JSON 标量，保 bool|int|str 联合类型)、
  `EnumValues TArray<FString>`。
- `FQuestLogicBranchRow`（QuestLogicBranches.json，`Branch`）：`Type`、`Quest`、`BranchId FString`、
  `FromStage FDataTableRowHandle`、`ToStage FDataTableRowHandle`、`Condition FString`、
  `Outcome FString`、`Effects FString`(JSON)。
- `FQuestRewardRow`（QuestRewards.json，`Reward`）：`Type`、`Quest`、`Ordinal int32`、
  `RewardKind FString`、`RewardValue FString`、`Amount int32`。
- `FQuestLogicRow`（QuestLogic.json，QuestLogic 的任务级标量）：`Type`、`Quest`、`Precondition FString`、
  `Unlocks TArray<FDataTableRowHandle>`。
- `FDialogueTreeRow`（DialogueTrees.json，`DialogueTree`）：`Type`、`Title FText`、`Quest`、
  `Participants TArray<FDataTableRowHandle>`、`RootNode FDataTableRowHandle`、`Tags TArray<FString>`、
  `Metadata FString`(JSON)、`Origin FString`、`ReviewStatus FString`、`SourceRef FString`(JSON)。
- `FDialogueTreeNodeRow`（DialogueTreeNodes.json，`DialogueNode`）：`Type`、`Tree FDataTableRowHandle`、
  `NodeId FString`、`Speaker FDataTableRowHandle`、`Text FText`、`NextNode FDataTableRowHandle`。
- `FDialogueTreeChoiceRow`（DialogueTreeChoices.json，`DialogueChoice`）：`Type`、
  `Node FDataTableRowHandle`、`Ordinal int32`、`Text FText`、`NextNode FDataTableRowHandle`、
  `Condition FString`、`Effects FString`(JSON)。

`OWCopilotRows.h` 始终生成上述全部结构（权威规格）。子表 DataTable 资产按需创建；发现是**形状驱动**的
（按 Name 键控的行数组），故推送路径无需改代码，只要在 `table_map` 里为要落库的子表补目标资产路径。

## 启用清单（一次性）

1. Edit > Plugins 启用 **Python Editor Script Plugin**，把 `OWCopilotBridge` 放进工程 `Plugins/`
   并启用（它会拉起 Python 插件依赖），重启编辑器。
2. 创建 DataTable 资产（Quests/Entities/Dialogues 各一），Row Struct 指向上面的结构；需要任务逻辑 /
   阶段 / 奖励 / 对白树全保真时，再按上一节为对应子表各建一张，Row Struct 指向对应 `F*Row`（加性，可按需）。
3. 需要无头推送时再启用 **Remote Control API**（HTTP 默认 30010 / WS 30020），或在
   Project Settings > Plugins > Python 勾选 **Enable Remote Execution**（多播 239.0.0.1:6766）。

## 三种导入路径

### A. 编辑器菜单（人工）
设 `OWCOPILOT_UE_EXPORT_DIR` 指向导出的 `unreal/` 目录，可选 `OWCOPILOT_UE_TABLE_MAP` 指向
`{"Quests": "/Game/.../QuestTable", ...}` 的映射 JSON，点菜单项即可。导入器读现有行、按 `Name`
合并、破坏性 empty-and-fill 回填、保存资产，并弹出结构化结果。

### B. Python Remote Execution（无头，多播 LAN）
```python
from owcopilot.integrations.unreal_remote import run_bundle_via_remote_execution
result = run_bundle_via_remote_execution("<export>/unreal", {"Quests": "/Game/QuestTable"})
```
向运行中的编辑器发送 `import owcopilot_bridge; owcopilot_bridge.import_bundle(...)`（复用 Epic 随
Python 插件分发的 `remote_execution.py`，不 vendor）。

### C. Remote Control HTTP（无头，结构化、防火墙友好）
```python
from owcopilot.integrations.unreal_remote import push_datatable_via_remote_control
push_datatable_via_remote_control("http://<host>:30010", "/Game/QuestTable", rows_json)
```
`PUT /remote/object/call` 直接调用 CDO 上的 BlueprintCallable 函数：
- 写：`FillDataTableFromJSONString` on `/Script/Engine.Default__DataTableFunctionLibrary`
- 存：`SaveAsset` on `/Script/EditorScriptingUtilities.Default__EditorAssetLibrary`

`rows_json` 是要回填的**整表**行数组；逐行 upsert 请先用
`owcopilot_bridge.merge_rows_by_name(existing, incoming)` 合并（`fill` 是破坏性 empty-and-fill）。

## 写路径的正规约定

- 读用 `export_data_table_to_json_string`，写用 `fill_data_table_from_json_string`（返回 `bool`，
  失败只记日志不抛异常，故**必须检查返回值**并给引导式提示）。
- `fill` 是「清空再填」——因此 upsert = read → merge-by-Name → refill，未触碰的行逐字节保留
  （由 `test_unreal_plugin.py` 回归证明）。
- 传入表的 Row Struct 作 `import_row_struct` 强制自动导入（无对话框），适配无头/CI。

## 仅真机可验证清单（live run pending，未声称已绿）

以下环节离线无法证实，须在真实 UE 机器上核对，纪律同既有 P3 "live run pending" 备注：

1. `fill_data_table_from_json_string` 是否真的接受本 JSON 对具体工程 Row Struct 完成类型强制
   （枚举 / soft-object ref / 嵌套结构）。
2. `text_mode="stringtable"` 下「裸 key 绑定 FText->StringTable」的精确 JSON 字面量随引擎版本而异。
3. ToolMenus 菜单是否渲染、文件/目录选择交互（stock UE 无一等 Python 文件夹对话框，菜单当前读
   环境变量路径；如需真正的选择器可加一个 Editor Utility Widget）。
4. `save_loaded_asset` / `SaveAsset` 是否真的写出 `.uasset`。
5. Remote Control 的 CDO 对象路径字面量、以及 `import_row_struct=None` 是否会在 HTTP 上弹对话框。
6. Python Remote Execution 多播握手（发现节点、`node_id`、命令回执）在真实网络下的往返。

## 离线已锁（CI 契约门）

- 导出器金样/往返：`tests/test_exporters_unreal.py`（形状锁 + 经 `recognize_engine_data` 闭环往返）。
- 插件：`tests/test_unreal_plugin.py`（`.uplugin` 内容型断言；无引擎可导入；read-merge-refill 逐行为
  保留未触碰行；`fill` 返回 `False`/资产缺失/无引擎均结构化引导；plan 排除 preflight）。
- 远程客户端：`tests/test_unreal_remote.py`（Remote Control PUT body 与 CDO 精确断言；fill→save 次序；
  Remote Execution 命令串与 `unattended/exec_mode`；模块缺失/无节点给引导式失败）。
