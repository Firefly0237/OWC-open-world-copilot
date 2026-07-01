# C3 细化设计：version 继承（copy-on-write）— 编排者起草

## C3 要交付（INV-3）
- 读 `(world, version)` 的有效内容 = 该 version 自有对象 **覆盖** 其 base 链上的对象（CoW：分支不复制全量，只存差异；读沿 base 链回退；tombstone 支持在子版本删除基线对象）。
- 有效 bundle 喂给索引/检索/审计 → audit/impact 也 scope 感知（兑现 audit/impact 侧"降N"，补上 C2 留的边界）。
- 分支创建 = 从 base 派生新 version（只写 registry + 空 diff，不复制全量）。

## 现状约束
- 内容文件 = 单一目录树（root/world/entities/…），**声明为 source of truth**；runtime SQLite 可从文件重建。
- runtime SQLite 已按 (world_id, version) 列分片（C1）；version_registry(world_id, version, base_version, created_at) 已建（C1）。
- snapshot = 整 bundle JSON 全量定格（不可变时间点，与 version 正交，INV-4）。

## 真正的分叉（需拍板）：version 差异内容存哪
**F. 文件覆盖层（推荐，守"文件为真值"）**
- 基线 version（如 v1）= 现有单一内容树。
- 派生 version（base=v1）= 一个**差异目录**，只放该 version 新增/改动的对象文件 + 一个 tombstone 清单（记录在本 version 删除的基线对象 id）。
- `load_scoped(world, version)` = 沿 base 链 [v, base(v), …] 从"基线树 + 各级差异目录"叠加解析：某 id 取链上最近 version 的定义；tombstone 移除。
- 优点：版本内容全部 file-backed、可 diff、守 source-of-truth；与现 ContentStore/save 一脉相承。
- 代价：改造 ContentStore.load→load_scoped（叠加解析）、save→写入当前 version 的差异目录；目录布局（如 root/versions/<version>/… 覆盖 root/… 基线）。中大工作量。

**S. SQLite 覆盖层（更简单，但破 source-of-truth）**
- version 差异存 runtime SQLite 表（world_id, version, ref, object_json | tombstone）。load_scoped = 基线 bundle + 该 version 覆盖行。
- 优点：不动文件树、实现简单。
- 代价：非基线 version 的内容**只在 runtime SQLite**（rebuildable 层），不 file-backed → 破"文件为真值"原则；版本内容无法像文件那样人工审阅/版本控制。**与项目哲学冲突。**

**H. 混合**：差异仍是文件（放 version 覆盖目录），但索引/解析统一走 SQLite。本质=F 的落地方式（F 已含）。

## 推荐 = F（文件覆盖层）
守住"文件为真值 + 可人工审阅 + 可 diff"，与项目现有 ContentStore/snapshot 哲学一致。代价是 load/save 改造较大——但这正是"完整多世界/版本"的核心，值得做实。

## C3 若选 F 的分步（各自可验/可提交）
- **C3a 版本解析层**：新增 `content/versioning.py` + `ContentStore.load_scoped(world, version)`（沿 base 链叠加基线树+差异目录，tombstone 移除）；无差异目录时=基线（默认 v1 无 base → 现行为逐字节不变，INV-1）。
- **C3b 分支创建 + save 到 version 差异目录**：`create_version(world, version, base)` 写 registry + 建空差异目录；save 写入当前 version 的差异层（基线 v1 仍写基线树）。
- **C3c audit/impact scope 感知**：ProjectContext 用 load_scoped 得当前 scope 有效 bundle → audit/impact 只跑当前 version 有效内容（兑现 audit/impact 降N）。
- 每步：执行→自查(INV-1/INV-3)→门禁绿→提交；深逻辑（base 链解析、tombstone）配足测试。

## 待拍板
1. 选 **F（文件覆盖层，推荐）** 还是 S（SQLite 覆盖层，简单但破 source-of-truth）？
2. 若 F：版本差异目录布局倾向 `root/versions/<version>/{world/entities,quests,…}` 覆盖 `root/…` 基线，OK 否？
3. C3 是否按 C3a/C3b/C3c 三小步走（推荐，稳）？
