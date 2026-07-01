# P0 工作组 2 — 整体验收 + 红线把关（G2 全量）

**验收 agent：只读、独立实跑。** 分支 `feature/scale-p0`，HEAD `e202e9a`，off master `a231a36`。
日期 2026-07-01。会话起始 git 快照里的 M/?? 文件（bm25.py/vector.py/sqlite.py/test_scope_dimension_c2.py）**已在 `938b959` (C2) 提交**，当前工作树 `git status` 干净——即验收对象就是提交 HEAD，无未提交残留。

---

## A. 门禁独立实跑（全部本机亲跑，不信提交信息）

| 门 | 结果 | 实证 |
|---|---|---|
| `pytest tests/ -q`（全量，含慢 usearch） | **PASS** | `1551 passed, 2 skipped, 7 warnings in 499.82s`（后台实跑，exit 0）。**逐字命中预期 1551/2。** 7 warnings 全是 test_dpo_export 的 `warn_empty=True` 空数据集提示，非错误。 |
| `ruff check src/ tests/` | **PASS** | `All checks passed!` |
| `mypy src/` | **PASS** | `Success: no issues found in 231 source files` |
| `owcopilot eval-acceptance` | **PASS（8/8 gate）** | `passed: true`。retrieval_hit_rate=**1.0**、retrieval_tight_hit_rate=**1.0**、impact_recall_100 passed、seeded_error_detection=**1.0**(25/25)、tool_selection_mean_f1=**1.0**、qa_citation/qa_faithfulness(opt-in skip, fail-closed)/clean_world_zero_fp 全 passed。65 实体/36 quest/72 本地化。 |
| `owcopilot eval-golden` | **PASS** | audit_no_open_errors(0/0/0)、retrieval_has_aldric、qa grounded(confidence 0.75, refused=false)、export_manifest_written、provenance_all_approved(4/4)。 |

门禁一栏：**全绿**，且检索召回 1.0 未因 G2-A/B/C 任何改动回退。

---

## B. 不变量 / 红线核查（逐条给证据）

### INV-1 默认 scope 逐字节 —— **PASS**
- **content_hash 字面锁**：`tests/test_scope_dimension_c1.py:88 test_content_hash_matches_pre_scope_baseline_4013691` 把 Entity/Quest/Relation 的 content_hash 钉成 baseline 4013691 的字面十六进制值（`fd212c3e…`/`2b0b47ec…`/`66ae7df1…`）。实跑该文件 **30 passed**。scope 若再泄漏进内容序列化会立刻炸这条。
- **scope 不入序列化**：`test_scope_is_absent_from_content_serialization` 断言各内容对象 model_dump 不含 world_id/version。
- **eval 召回 1.0**：见 A 段两个 eval 门。
- 结论：默认 (default,v1) 下 content/检索/审计/eval 与 pre-scope 等价。

### INV-2 scope 仅存储层，不入内容模型 —— **PASS，但权威性有一处例外见"必修 1"**
- **模型层干净**（实证 `content/models.py:51-61`）：`ProvenanceMixin` 只有 origin/source_ref/review_status，**无** world_id/version。Entity 自带的 `version`(line 72) 是既有自由文本内容字段，非 scope（注释与 `test_entity_keeps_its_own_content_version_field` 均明确）。
- **PK 含 scope**（实测 PRAGMA）：content_index=`(world_id,version,ref)`、reference_sources=`(world_id,version,id)`、reference_chunks=`(world_id,version,ref)`、version_registry=`(world_id,version)`。graph_edges 用 AUTOINCREMENT id + 作用域唯一索引 `(world_id,version,edge_fingerprint)`。**均正确。**
- **replace_*/读/DELETE scope 过滤**（`storage/sqlite.py` grep 实证）：replace_content_index(973/989/993)、replace_graph_edges(1432/1442)、replace_reference_index(1535/1625/1631/1640)、get_vectors/prune_vectors/relation_rows_for_entities/search_content 等**全部带 `WHERE world_id=? AND version=?`**。
- **跨 scope 写不删别 scope**（实测 test 通过）：`test_cross_scope_write_does_not_delete_other_scope_content`、`test_cross_scope_graph_edges_isolation`、`test_same_ref_coexists_across_scopes_in_authoritative_pk` 均绿。
- **检索读 scope 过滤**（C2，实证 `retrieval/vector.py:59-63,74-78` + `test_scope_dimension_c2.py`）：bm25/vector/relation-completion/reference-hybrid 读全过滤当前 scope；含 **实测"降N"** 测试 `test_fallback_scan_N_shrinks_with_scope`（计数证明每 scope 扫描行数严格 < 全表）。
- **例外（必修 1）**：`content_vectors` / `reference_vectors` 两张 **blob 缓存表 PK = `(ref, model_id)`，不含 scope**（其余权威表都含）。它们*带* scope 列且读*按* scope 过滤，但**写路径 `upsert_vectors` 用 `ON CONFLICT(ref, model_id)`**，所以同 DB 里同 ref 跨 scope 写会互相覆盖并把行重新 stamp 成后写者的 scope。详见下节实证与严重性判定。

### INV-3 CoW 继承 —— **PASS**
（`content/store.py` + `test_version_overlay_c3.py` 全绿）
- load_scoped 沿 base 链叠加（`_resolve_version_chain` 100-131）、override 胜、tombstone 移除、relations union（`_apply_overlay` 163-197）。
- **cycle 防御**：链解析有 `seen` 集（125）、`create_version` 显式拒环（217）。测试 `test_version_chain_cycle_is_defensive`、`test_create_version_rejects_bad_inputs`。
- **save_scoped 只写 diff**（CoW 最小化）：`_diff_bundle` 只写覆盖/新增对象 + tombstone，未变对象不复制进 override；`test_save_scoped_writes_only_the_diff`、`test_save_scoped_roundtrips_override_add_and_drop` 验证 round-trip。
- 覆盖用例齐全：override-wins / add-new / tombstone-removes / multi-level-nearest-wins / tombstone-then-readd / relations-union。

### INV-4 snapshot 与 version 共存 —— **PASS**
（`content/snapshot.py` + `test_version_diff_snapshot_c4.py` + `test_merge_c4c.py` 全绿）
- **snapshot 记 scope**：`SnapshotMeta.world_id/version`(48-52)，`write_snapshot` 冻结 `load_scoped(scope)`；`test_snapshot_freezes_version_and_records_scope`。
- **冻结 version 有效 bundle**：snapshot 存的是 CoW 解析后的 effective bundle。
- **与 version 共存 + 向后兼容**：`list_snapshots` 只取存在的键，pre-C4 无 scope 字段的 snapshot 回落默认 scope；`test_list_snapshots_backward_compat_pre_c4`。
- **merge 三路**（C4c）：`merge_versions` 以 common base 三路合并，一侧改动 auto-resolve、两侧冲突（add-add/both-changed/modify-delete）**只收集不自动写 canon**（keep ours 占位）。`test_merge_c4c.py` 覆盖全部三种冲突 + auto-resolve + common-base。红线"冲突走人审、不自动写 canon"成立。

### 真实落地非搭壳 —— **PASS**
- **G2-A int8 两阶段**（`vector_backend.py:400-568`，真实现）：INT8[dim] vec0 粗召回 k'=max(3·limit,30) → fp32 sidecar 精排。`test_int8_two_stage_recall_is_near_lossless_and_beats_int8_only` 断言 **two-stage recall ≥0.99 且严格 > int8-only**；`test_int8_index_is_smaller_than_fp32`；`test_int8_two_stage_holds_acceptance_retrieval_gate`（eval 1.0）。
- **G2-B usearch 真 HNSW**（`vector_backend.py:634-938`，真实现）：固定调参 **connectivity=32/expansion_add=200/expansion_search=2048**（代码常数 585-587，注释详述 512 seed-fragile 故上调 2048）；ref↔uint64 keymap（blake2b + 线性探测，折 scope 进 hash）；self-heal（key 数≠keymap 数则从 fp32 权威表重建，`test_usearch_dirty_index_rebuilds`、`test_usearch_corrupt_index_file_rebuilds`）；按 N 阈值 `USEARCH_MIN_N` 切（小 N 走 sqlite-vec，`test_..._small_corpus_stays_exact`；大 N 切 usearch，`test_..._large_corpus_switches`）；import 失败 guided 回退（`test_..._usearch_unavailable_falls_back`）。tuned two-stage recall ≥0.95 且 ≥default-param。本机 `import usearch.index; import sqlite_vec` 均 OK。
- **G2-C load_scoped/save_scoped/merge 真跑**：见 INV-3/4，非包壳。
- **vec0 PARTITION KEY 真分区**：`SqliteVecBackend`/`Int8Backend` 建表 `world_id TEXT PARTITION KEY, version TEXT PARTITION KEY`（308-310/449-450），每 search/upsert/count 带 scope。

### 不静默降级 / 根因修不打补丁 / 向后兼容 —— **PASS**
- 所有降级路径 guided：sqlite-vec 不可用 → `logger.info(... using the in-memory numpy vector backend)` 返 None 回退（1160-1166）；usearch 不可用 → guided log 回退 sqlite-vec（1206-1212）；两者构造异常都收成 guided error 而非裸崩。
- **向后兼容迁移真做**：`_ensure_column`/`_ensure_scoped_pk`/`_rebuild_fts_if_unscoped` 就地升级 legacy DB，scope 列 default 'default'/'v1' 回填不丢行；`_drop_unpartitioned_vec0`/`_plain_table_lacks_scope` 把 pre-C1 派生索引 drop 后从 blob 权威表重建（不丢 canon）。`test_scope_dimension_c1.py` 有 legacy-DB 迁移用例（`_build_legacy_db`）。
- **C5/C6 诚实标注成立（非偷懒）**：
  - C5 `92d1449` 提交信息明说"property already delivered by C1/C2/C3c/G2-B"，**不造冗余层**，只加 ProjectContext 级 residency 回归测试（indexed v2 后开 v1 只载 v1 bundle+graph、检索不出 v2 行），并**显式把 cold-archival 推到 P1** 并给理由（行已 disk-resident 非 RAM，reduce-N 目标已达）。测试实测通过。
  - C6 `e202e9a`：`open_managed_world` 把世界名→world_id、version 选 CoW 线，默认 `sqlite_path=":memory:"`（每世界独立运行 DB），内容根本就是各自独立目录。docstring 诚实标注"even if a future deployment shares one runtime DB across worlds, the world_id column keeps them apart"——**这正是必修 1 会咬到的场景**。

### 跨单元交互 —— **PASS（无相互踩坑）**
- G2-A/B 后端 + G2-C scope 绑定叠加：vec0 每 scope 一 PARTITION、int8 fp32 sidecar/usearch fp32+keymap 均 `(world_id,version,ref)` 复合键、usearch `.usearch` 文件每 scope 一份（`_usearch_index_path` 默认 scope 保持原文件名、非默认加 `.{world}.{version}` 后缀）。三者搜索结果互不泄漏——E2E 实测（见下）双 scope 同 ref 共享 DB，scope A 重开检索仍返回自己的 body 不串 scope B。
- ProjectContext 每次 open 绑定单一 scope，无进程内多 scope 混索引同 DB 的路径（除非外部显式传同一 sqlite_path，即必修 1 场景）。

---

## 必修 1（唯一一条）：blob 缓存表 PK 缺 scope —— 潜伏中 / 未触发 / 中危

**事实**（实测 PRAGMA + 探针）：
```
content_index     PK=(world_id,version,ref)   ✓
reference_sources PK=(world_id,version,id)    ✓
reference_chunks  PK=(world_id,version,ref)   ✓
version_registry  PK=(world_id,version)       ✓
content_vectors   PK=(ref,model_id)           ✗ 缺 scope
reference_vectors PK=(ref,model_id)           ✗ 缺 scope
```
探针复现：同一 DB，scope A 写 ref="x"→scope B 写同 ref="x"（`ON CONFLICT(ref,model_id)`）→**覆盖并重 stamp 成 B**；随后 `get_vectors(scope=A)` 返回空（该 ref 的行已归 B）。E2E 探针：两 scope 同 ref entity:x，blob 表最终只剩 **1 行**（应 2 行）。

**严重性判定 = 中（不是高）**，理由三条：
1. **不是正确性 bug**。E2E 实测确认：即便 blob 缓存被跨 scope 覆盖，**检索结果仍正确**——vec0/int8/usearch 三索引都按 scope 正确分区，且 `_reindex` 在 blob 缓存 miss 时按当前 scope 的 content_index body **重新 embed 正确文本**。scope A 重开检索 'dragon' 仍返回 Dragon body，不串 B 的 village body。
2. **真实影响是缓存抖动（scale 反效果）**：共享 DB 且跨 scope 撞 ref 时，被别 scope 覆盖过的 ref 每次 reload 都**从头重 embed**，增量 embedding 缓存失效。用 HashingEmbedder 无痛，用真语义模型(bge-m3)= 白烧算力——**恰好抵触本 P0"增量、不重扫"的 scale 目标**。
3. **当前 shipped 默认路径未触发**：C6 `open_managed_world` 默认 `:memory:`（每世界独立 DB），单世界项目也只有默认 scope，所以生产默认路径下不会撞。但**它字面违反 INV-2 的权威表约束**（"content_vectors/reference_vectors 带 (world_id,version)…权威表 PK 必须含 scope…跨 scope 写不删别 scope"），且 C6 docstring 自己已把"未来共享 DB 跨世界"列为设计目标——那一刻此 bug 就从潜伏转活跃。而 CoW 版本继承(INV-3)天然让子版本与 base **共享同一批 ref**，是最容易撞的场景。

**测试盲区实证**：`test_same_ref_coexists_across_scopes_in_authoritative_pk` 只测了 content_index；**没有** content_vectors/reference_vectors 的同 ref 跨 scope 用例（有的话会红）。C2 的跨 scope 隔离测试全用**不同 ref**，故从未触发此 PK 撞。

**修法（根因，不打补丁）**：把两表 PK 改为 `(world_id, version, ref, model_id)`，`upsert_vectors` 的 `ON CONFLICT` 同步改为四列；走既有 `_ensure_scoped_pk` 风格的就地重建迁移（与 content_index 完全同构，已有范式）。补一条 content_vectors 同 ref 跨 scope 共存测试锁住。改动小、隔离、有现成迁移范式。

---

## 结论

G2（A int8 两阶段 / B usearch ANN / C 多世界·版本分片 C1–C6）**工程质量高、真实落地非搭壳、门禁全绿（1551 passed、ruff/mypy 净、eval 召回 1.0）、INV-1/3/4 完全成立、不静默降级、C5/C6 诚实标注属实**。唯一缺口是 INV-2 权威表约束的一处例外：两张 blob 缓存表 PK 未含 scope，属**潜伏中、当前默认路径未触发、中危**的缓存正确性/一致性问题（正确性不受影响，但违反 INV-2 字面契约且会在共享-DB/CoW 场景下变成缓存抖动，抵触 scale 目标）。

**最终一句话：有 1 条必修（content_vectors/reference_vectors 的 PK 补 scope，与 content_index 同构迁移 + 补同-ref-跨-scope 测试）；其余全部 ACCEPT。**
