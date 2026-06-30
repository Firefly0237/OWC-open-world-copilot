# G2-C 科学工作分解（full multi-world/version 分片）

目的：把"完整多世界/版本数据模型"分解成**契约清晰、依赖明确、各自可独立提交且失败可隔离**的工作单元。原则：先定不变量与契约（骨架），再按依赖 DAG 排单元，每单元给精确文件领地 + 提供/消费的契约 + 向后兼容不变量 + 验收判据 + 风险 + 隔离/回滚。

---

## 第 0 层：不变量与契约（所有单元的地基，不可违反）

**INV-1 向后兼容（硬）**：scope 默认 `(world_id="default", version="v1")`。在默认 scope 下，content/检索/审计/impact/eval **逐字节等价**于 G2-B 状态（1492 tests、eval 召回 1.0 不变）。任何单元都不得破坏这条。

**INV-2 scope 全程携带（仅存储层，不入内容模型）**：scope 是**存储/分片维度，不是内容字段**——**绝不**加到 ProvenanceMixin/内容模型（否则泄漏进 model_dump/content_hash/snapshot/save，破 INV-1，已被 C1 复查实证）。每条持久化行（content_index/graph_edges/reference_*/content_vectors/reference_vectors/vec0）带 `(world_id, version)`，由 **store 的当前 scope（ProjectContext 设）在写入时 stamp**，对象本身不携带。**权威表 PK 必须含 scope**（如 content_index PK=(world_id,version,ref)）；**所有 replace_*/读/DELETE 必须 scope 过滤**（否则跨 scope 删数据，已被复查实证）。每次读要么显式给 scope、要么用 store 当前 scope；缺省=default。

**INV-3 版本继承（读语义）**：读一个 `(world, version)` 的内容 = 该 version 自有对象 **覆盖** 其 base 链上的对象（copy-on-write：分支新版本不复制全量，只存差异；读时沿 base 链回退）。`(default, v1)` 无 base → 退化为单 scope（满足 INV-1）。

**INV-4 snapshot vs version 统一**：
- **version** = 可变的命名内容线（有 base、可写、copy-on-write）。
- **snapshot** = 某 (world, version) 在某时刻的**不可变全量定格**（现 content/snapshot.py 的语义）。
- 统一关系：snapshot 是"对某 version 打的只读定格点"；version 是"活动可写线"。二者不互相替代——version 提供分片+继承，snapshot 提供时间点回溯。C4 落实它们共存且不冲突（snapshot 记录其所属 scope）。

**契约对象 `Scope`（C1 引入，全程复用）**：`Scope(world_id: str, version: str)`，`DEFAULT = Scope("default","v1")`。版本注册表 `version_registry(world_id, version, base_version|None, created_at)`（C1 建表，C3 用 base 链）。

---

## 依赖 DAG（决定排期与可并行性）

```
C1 (scope 地基: 模型+schema+迁移+写路径携带 scope + version_registry 表)
      │  提供: INV-2 的存储契约 + Scope 对象 + version_registry
      ├──────────────► C2 (scope-aware 读: 检索/审计/impact 默认 scope 过滤)  [兑现"降N"]
      │                      │ 提供: 单 version 内的 scoped 读
      │                      ▼
      ├──────────────► C3 (版本继承: 读沿 base 链 + copy-on-write 分支创建)   [INV-3]
      │                      │ 提供: 跨 base 链的有效内容视图
      │                      ▼
      ├──────────────► C4 (跨版本 diff/merge + snapshot↔version 统一)         [INV-4]
      │                      ▼
      └──────────────► C5 (冷热分层: active 常驻 / 历史 lazy 挂载)
C6 (多世界 CRUD/切换 + CLI/API)  仅依赖 C1
```
- **串行主线**：C1 → C2 → C3 → C4 → C5（语义层层叠加）。
- **可并行**：C6 只依赖 C1，可与 C2/C3 并行（不同文件领地：C6=world 管理/CLI，C2/C3=检索/读语义）。
- **失败隔离**：每单元独立提交且默认 scope 下绿；某单元进程崩 → 只回滚该单元的工作树，已提交单元不受影响（这正是分小单元的科学理由）。

---

## 逐单元规格（真正的"分工"）

### C1 — scope 地基【进行中】
- 领地：content/models.py、storage/sqlite.py、retrieval/vector_backend.py、pipeline/project.py。
- 提供契约：INV-2 存储 + `Scope` + `version_registry` 表。
- 改动：ProvenanceMixin 加 world_id/version（默认 default/v1）；6 张表加 scope 列+复合索引；vec0 PARTITION KEY=(world_id,version)；旧库迁移归 default/v1；写路径填 scope；建 version_registry 表（即使 C3 才用）。
- 向后兼容不变量：INV-1（默认 scope parity）。验收：迁移不丢数据测试 + 默认 scope parity（eval 1.0）+ scope 列真分区测试 + 全门禁。
- 风险：schema 迁移/写路径遗漏 scope → 中。隔离：默认 scope 下零行为变化，可独立提交。

### C2 — scope-aware 读（兑现"降N"）
- 领地：retrieval/（bm25/vector/graph/context_pack/fusion 入口）、audit/runner+context、impact/analyzer、mcp_server/tools 的读工具、pipeline/project（把当前 scope 传到读路径）。
- 消费 C1 契约；提供：读默认只在当前 scope（单 version）内。
- 不变量：未指定 scope=default → 与 C1 后行为一致（eval 单 scope，门 1.0）。验收：构造 2 个 scope 的数据，证明检索/审计只返回当前 scope；eval 门仍 1.0；性能：N 随 scope 缩小而降（可计数/计时实证"降N"）。
- 风险：读路径多、易漏一处 → 中高。隔离：默认 scope 下不变；可独立提交。

### C3 — 版本继承（INV-3）
- 领地：新增 version 解析层（如 content/versioning.py）、store 读接口（按 base 链合并）、版本创建 API（copy-on-write 分支）。
- 消费 C1（version_registry）+ C2（scoped 读）；提供：跨 base 链的有效视图 + 分支创建。
- 设计要点：读 `(w,v)` → 解析 v 的 base 链 [v, base(v), base(base(v))…]；对每个对象 id 取链上最近的定义（version 覆盖 base）；删除标记（tombstone）支持"在子版本删除基线对象"。分支创建只写 version_registry + 该 version 的差异行，不复制全量。
- 不变量：无 base 的 version（如 v1）= 单 scope（INV-1）。验收：base 链覆盖/回退/tombstone 正确性测试；分支创建不复制全量（计数证明）；默认 v1 行为不变。
- 风险：语义最深 → 高。隔离：仅当存在多 version 时启用；默认 v1 无 base 退化。**执行前本单元再出一份细化设计**（base 链解析的确切算法 + tombstone 表示 + 与 C2 scoped 读的交互）。

### C4 — 跨版本 diff/merge + snapshot 统一（INV-4）
- 领地：content/snapshot.py、新增 version diff/merge、storage（snapshot 记 scope）。
- 消费 C1/C3；提供：版本间 diff（复用 bundle_diff，scoped）、merge（把一个 version 的差异并入另一个，冲突策略）、snapshot 记录所属 scope。
- 设计要点：明确 snapshot（不可变定格）与 version（可变线）共存；diff = 解析两个 scope 的有效视图后 bundle_diff；merge 的冲突=同 id 在两版本不同定义→人审（沿用 review 范式，不自动写 canon）。
- 验收：版本 diff 正确、merge 冲突走人审、snapshot 跨版本回溯正确。风险：中高。隔离：不影响单版本路径。

### C5 — 冷热分层
- 领地：storage（按 scope 的索引挂载/卸载）、ProjectContext（active scope 的索引常驻，历史 lazy）。
- 消费 C1-C3；提供：只 active version 索引常驻内存/打开；历史 version 检索按需挂载其分片（vec0 PARTITION + usearch per-shard 索引）。
- 验收：active 切换时索引正确挂卸载；历史检索 lazy 加载；内存只随 active scope。风险：中。隔离：性能层，不改正确性。

### C6 — 多世界 CRUD/切换（仅依赖 C1，可并行）
- 领地：world 管理（create/list/switch）、CLI（如 `owc world ...`）、service/api 端点、ProjectContext 选 world。
- 验收：建/列/切换世界；切换后读写落到对应 world scope；默认 world=default。风险：低-中（偏工程）。隔离：独立领地，可与 C2/C3 并行。

---

## 全局验收 + 收口
- C1-C6 各自执行→复查→门禁绿后提交；C 全完 → 组 2 整体验收 agent + 监督 agent（红线/跨单元交互/降N 实证）→ 总结归纳 agent 收口整个 P0 程序。
- 贯穿红线：真实落地非搭壳、INV-1 向后兼容、不静默降级、$0、每单元可独立提交绿、C3/C4 执行前各出细化设计。

## 执行方式
- 默认：每单元一个执行 agent（后台）+ 一个复查 agent；编排者每次于 agent 死亡时自查残留状态（已验证有效）。
- 若某单元 agent 连续失败：编排者**前台分步亲做**该单元（每步可验、可提交），按本工作计划的该单元规格执行。
