# P0 / G2-C1 复查报告（独立复查 agent，只读）

复查对象：C1「scope 维度基础」未提交工作树改动（分支 `feature/scale-p0`，HEAD=G2-B `4013691`）。
基准：`P0_G2C_WORKPLAN.md`（INV-1/INV-2 + C1 规格）、`P0_G2C_DESIGN.md`。
领地实际改动：`content/models.py`、`storage/sqlite.py`、`retrieval/vector_backend.py`、`pipeline/project.py` + 新测试 `tests/test_scope_dimension_c1.py`。

## 结论：返工（3 条，其中 2 条硬）

门禁全绿，无 C2/C3 越界——但存在 1 个**缺失的必交付物**、1 个**真实 INV-1 逐字节违规**、1 个**真实 INV-2 存储契约缺口（跨 scope 数据丢失）**。这些都不被现有测试/门禁捕获（all green 是因为没有任何测试 pin 字面 hash、且 eval 世界都是单 scope 重新生成），属本项目明令禁止的「静默降级」。

---

## 实跑门禁（都过，但不能掩盖下列缺陷）
- `pytest tests/ -q` → **1505 passed, 2 skipped**（557s），与预期一致。
- `ruff check src/ tests/` → All checks passed。
- `mypy src/` → Success, no issues found in 231 source files。
- `eval-acceptance` → 顶层 passed:true；retrieval_hit_rate=1.0、tight=1.0、impact_recall_100 过、detection_rate=1.0、tool F1=1.0。
- `eval-golden` → 顶层 passed:true；5 check 全过。
- `test_scope_dimension_c1.py` 单独 → 13 passed。
- G2-A `test_int8_two_stage_holds_acceptance_retrieval_gate`（hit_rate==1.0/tight==1.0）→ 过。

---

## 返工项 1【硬 / INV-1 逐字节违规】scope 字段泄漏进 content_hash / snapshot / 磁盘内容文件

`content/models.py:65-66` 给 `ProvenanceMixin` 加了 `world_id: str="default"`、`version: str="v1"`。二者**非 None**，而全项目序列化用 `model_dump(mode="json", exclude_none=True)`，所以这两个字段**进 dump**。实测（同输入对象，工作树 vs G2-B 基线 `4013691`）：

| 对象 | G2-B content_hash | C1 content_hash | 变了? |
|---|---|---|---|
| entity | `0fe0790a…` | `4369bb57…` | **是** |
| quest | `d9feb1f8…` | `4a01087e…` | **是** |
| relation | `66ae7df1…` | `47adc2a8…` | **是** |

- dump 实测：entity 多了 `"world_id":"default"`（其自有 `version` 仍 None 被排除）；quest/relation 多了 `"world_id":"default"` **和** `"version":"v1"`。
- 执行 agent 在交接里声称「entity content_hash/snapshot 逐字节不变」——**不成立**：entity 的 `world_id` 仍泄漏，hash 已变。
- 三处泄漏面：`content/hash.py:14` content_hash、`content/snapshot.py:80` snapshot payload、`content/store.py:257` `_write_json`（=`ContentStore.save()` 写回**每个 authored JSON 文件**会新增这两个字段，磁盘内容文件表示改变，最直观的 INV-1 违规）。
- 为何门禁仍绿：无任何测试 pin 字面 hash 值（`grep` 确认 tests 只断言 hashA==hashB / len==64 / round-trip 相等），且 in-process round-trip 稳定（新字段两端都 default 进来，bundle_diff 无幻象 diff）。**真实破坏面**=任何**落盘**的 pre-C1 hash（旧 audit baseline、旧 snapshot 的 `content_hash` 字段、旧 export manifest）重算即不匹配，使其失效。
- 内部矛盾：`models.py:62-64` 注释自承「C1 用 store 当前 scope 驱动分片、**不用** per-object 字段」，但仍加了会泄漏序列化的 per-object 字段——这两个 model 字段对 store-driven 分片是多余的，却破坏了逐字节等价。
- 修法（任选）：给这两个字段加 pydantic serializer 在 dump 时排除默认值；或彻底不在 model 上加 scope 字段、纯 store 层携带（与其自身注释一致）。`test_scope_dimension_c1.py:75` 的 `test_entity_free_text_version_is_unchanged` 只断言 entity dump 无 `version`，**漏断言** `world_id` 不在 dump——返工须补「content_hash 对三类对象 == G2-B 值」的回归断言。

## 返工项 2【硬 / INV-2 存储契约缺口】权威关系表不能共存两个 scope，跨 scope 写删除他 scope 数据

`storage/sqlite.py`：6 张表加了 scope **列 + 复合索引**（✓ 规格字面满足），但权威关系表 PK **未**纳入 scope：
- `content_index` PK=`ref`（:143）；`content_vectors`/`reference_vectors` PK=`(ref,model_id)`（:161）；`reference_sources` PK=`id`；`reference_chunks` PK=`ref`。
- 只有派生索引（vec0 / int8 fp32 sidecar / usearch fp32+keymap，`vector_backend.py`）把 scope 纳入 PARTITION KEY / PK。

后果（实测复现）：`replace_content_index`（:773-788）的 `existing_hash` / `removed` / `DELETE FROM content_index WHERE ref=?` **全程无 scope 过滤**。同一 DB 先写 `default`（entity:a）再开 `w2` 写 entity:b：

```
after default write: [('entity:a','default')]
after w2 write, ALL rows: [('entity:b','w2')]    # entity:a 被删！
```

→ 写非默认 scope **会摧毁**同库默认 scope 的 content_index 行。`replace_reference_sources`（:1320 起，读/删同样无 scope 过滤）同病。
- 不破 INV-1：单世界（仅 default）项目永远只有一个 scope，行为不变（故全门绿）。
- 但**真的破 INV-2**：prompt #2「写非默认 scope w2/v3 真落到对应行/分区」——w2 行落了，却跨 scope 删了 default。C1 自称交付「INV-2 存储契约」，权威表层面**未交付**多 scope 共存；C2/C6 在其上写多 scope 会静默丢数据。
- 漏测：`test_replace_writes_stamp_the_store_scope`（:211）只往**全新库**写**单个** w2 scope，从未在一个库里放两个 scope 跑 content_index，所以测不到跨删。
- 注：vec0 PARTITION KEY 隔离是**真**的（`test_vec0_partition_key_isolates_scopes`/int8 版都实证两 scope 不串，✓）；缺口在权威关系表 + replace_* 写路径。
- 修法：权威表 PK 改 `(world_id,version,ref[,model_id])`；`replace_content_index`/`replace_reference_sources` 的 existing 读与 removed DELETE 加 `AND world_id=? AND version=?`。

## 返工项 3【缺失必交付物】version_registry 表未建

- 工作计划 line 20（契约对象）：`version_registry(world_id, version, base_version|None, created_at)`「**C1 建表**，C3 用 base 链」；line 51 C1 规格再次明列「**建 version_registry 表（即使 C3 才用）**」。
- 全仓 `grep version_registry` 仅命中工作计划文档本身，源码 **零**命中。`sqlite.py` 的 CREATE TABLE 清单无此表。
- 这是 C1 明确交付物，C3 依赖它。简单（4 列建表 + IF NOT EXISTS），但必须补，且应有建表测试。

---

## 核到位、判 PASS 的项（供参考）
- **迁移安全（✓）**：`_ensure_column`（:341）用 `ALTER TABLE ADD COLUMN ... DEFAULT` 在位升级、SQLite 自动 backfill 旧行到 default/v1、幂等（先查存在再加）。`test_legacy_db_upgrades…` + `test_migration_is_idempotent` 实测过。派生 vec0/usearch 旧 schema drop+从权威 blob 重建——blob cache 列是 ADD 不是 drop，故无权威数据损失，推理成立。
- **vec0 PARTITION KEY 真隔离（✓）**：`vector_backend.py` vec0/int8/usearch 三类后端实例绑定单 scope，upsert/search/vector_for/count 全 scope 约束；两 scope 隔离测试实证。
- **Entity.version 冲突（部分✓）**：自由文本 `version: str|None=None`（:82）确实保留、shadow 了 mixin scope version、entity dump 无 `version`、`# type: ignore` 合理。但见返工 1——entity 的 `world_id` 仍泄漏，故「entity 逐字节不变」总体**不成立**。
- **无越界（✓）**：`audit/` 零 scope 改动；retrievers（vector.py/bm25/graph_expand/context_pack/fusion）零 scope 过滤——C2 读过滤未偷做。无 base 链解析/继承/copy-on-write/tombstone（C3）逻辑。store 层 get_vectors/vector_count/prune_vectors 按本 store scope 过滤属 C1 存储载体（非跨 scope 读过滤），不算越界。默认全量读仍跨 scope（因只有 default 数据）。

---

## 一句话
**返工**：3 条——(1) scope 字段泄漏进 content_hash/snapshot/磁盘内容文件，破 INV-1 逐字节等价（`content/models.py:65-66`）；(2) 权威关系表 PK 未纳 scope + replace_* 写路径无 scope 过滤，跨 scope 写删他 scope 数据，INV-2 存储契约未真交付（`storage/sqlite.py:143/773-788/1320`）；(3) 必交付物 `version_registry` 表未建（`storage/sqlite.py` 缺）。
