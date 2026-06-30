# P0 工作组 2 — G2-A 独立复查报告（int8 两阶段精排）

**复查 agent（只读，不改代码）。** 对象：工作树未提交改动（分支 `feature/scale-p0`，组 1 已提交至 3635771）。
所有结论本机实跑取证（项目 venv `F:\openworld\.venv`），不只看断言。

变更面（`git diff --stat`，仅 4 个 src/test + 1 program 文档，491 行）：
- `src/owcopilot/retrieval/vector_backend.py`（+158，**纯 append**，新增 `quantise_int8` + `SqliteVecInt8Backend`）
- `src/owcopilot/retrieval/vector.py`（+7，`VectorRetriever(quantized=...)` 透传）
- `src/owcopilot/storage/sqlite.py`（+58，`make_vector_backend(quantized=...)` + `_vec0_int8_table` 白名单 + backfill 泛化）
- `tests/test_vector_backend.py`（+272，14 个新测）
- `scratchpad/P0_PROGRAM.md`（文档，记 G2 范围）

---

## 逐条核查（每条带证据）

### 1. 真两阶段非搭壳 — ✅ PASS
`SqliteVecInt8Backend.search`（vector_backend.py:385-418）确为真两阶段：
- 阶段①：`k = min(count, max(3*limit, 30))`（:395，`_INT8_COARSE_MULTIPLIER=3` / `_INT8_COARSE_FLOOR=30`），int8 粗召回 `WHERE embedding MATCH vec_int8(?) AND k = ? ORDER BY distance`（:397-401）——查询确用 `vec_int8(?)` 包裹（:399），存储确为 `INT8[dim]` vec0 列（`_ensure_tables` :339-342）。
- 阶段②：从 `{table}_fp32` sidecar 取这 k' 个候选的 fp32 向量，按精确 `stored @ q` 精排，再 `_rank` top-limit（:407-418）。重 fp32 工作只摸 k' 行，非全语料。
- `vector_for`（:420-427）从 fp32 sidecar 返回精确 fp32，**int8 损失不泄漏给上游 rerank**。实测取证：`vector_for("x")` 与归一化输入 `allclose(atol=1e-6)=True`，且与 int8 反量化值 `not allclose=True`（确证返回的是 fp32 真值而非反量化）。
- 与 fp32 后端排序一致性：构造 N=60/k'=30≥语料一半，two-stage 与 `SqliteVecBackend` top-5 排序 50 查询 **0 处不一致**。

### 2. 召回实证真实 — ✅ PASS（自跑确认数字，非只看断言）
`test_int8_two_stage_recall_is_near_lossless_and_beats_int8_only`（test:336-385）：真测 recall@10 对 fp32 ground truth（`_fp32_truth` :320-330 是精确余弦 top-k），int8-only baseline 真用单阶段 `k=limit` 无 rerank（test:368-375），断言 `two_stage_recall >= 0.99` 且 `> int8_only_recall`。
**我独立复跑同种子语料（dim64/N1500/24簇/120查询）**：
| 路径 | recall@10 |
|---|---|
| 两阶段（backend.search 真路径） | **1.0000** |
| int8-only 单阶段 | 0.9333 |
| fp32 后端（精确，sanity） | 1.0000 |
两阶段 ≥0.99 ✅、严格 > int8-only ✅（margin 1.0 vs 0.933，确定种子非饱和并列，断言不脆）。
`test_int8_two_stage_holds_acceptance_retrieval_gate`（test:516-553）真用 int8 后端注入 `ContextPackBuilder` 跑 `_retrieval_hit_rate`，断言 `hit_rate==1.0` 且 `tight_rate==1.0`——真锁 eval 召回门。该测在套件中通过。

### 3. 默认安全 — ✅ PASS
实测：`make_vector_backend("hashing-1024", dim=1024)` → `SqliteVecBackend`（fp32）；`quantized=True` → `SqliteVecInt8Backend`。默认 `VectorRetriever(store)._quantized == False`。int8 是显式 opt-in，不会无意变默认。表名隔离：fp32=`content_vec`、int8=`content_vec_i8`、sidecar=`content_vec_i8_fp32`（`_VEC0_INT8_TABLES` sqlite.py:1542），二者可同库共存无碰撞。

### 4. 量化正确性 — ✅ PASS
`quantise_int8`（vector_backend.py:53-64）= 先归一化、`round(x*127)` clip 到 `[-127,127]`、`int8`。
- 对称：`q(-v) == -q(v)` ✅（用 127 非 128，无 -128 离群）。
- 确定性/纯：同输入同字节 ✅。
- 零向量 → 全零 ✅（归一化对零 no-op，round(0)=0）。
- 极值：one-hot 活分量饱和到 127、`1e9/-1e9` 等大幅先归一化故不溢出 ✅（`test_quantise_int8_handles_zero_and_extreme_vectors` + 我复跑）。
- 零查询 `search`：`vec_int8`(全零) 不崩，返回结果（无害——backfill 空表探测先走 `count==0`）。

### 5. 复用既有抽象 — ✅ PASS
`SqliteVecInt8Backend` 结构合 `VectorSearchBackend` Protocol（upsert/delete/search/vector_for/clear 全齐，签名一致）；复用共享 `_normalise`/`_rank`/`SqliteVecBackend._load_extension`（:334）。fp32 源为独立 sidecar 表（非组 1 的 `content_vectors` blob 表本身，但同语义、backfill 仍从 `content_vectors` 量化填充——见下「说明」）。组 1 的 `SqliteVecBackend`/`NumpyMatrixBackend`/numpy 回退/对拍**未改一行**（diff 对该文件为纯 append，无任何既有行删改）。

### 6. 无回归无越界 — ✅ PASS
- `pytest tests/ -q`：**1481 passed, 2 skipped**（与期望门一致）。
- `pytest tests/test_vector_backend.py`：27 passed。
- 召回门：`pytest tests/test_acceptance_eval.py tests/test_golden_evaluation.py`：10 passed（两个 retrieval 召回门仍 1.0）。
- ruff（4 文件）：All checks passed。mypy（3 src）：Success, no issues。
- 越界检查：tracked src diff 中 **无** usearch/shard/partition 任何引用（仅 scratchpad 探针与 program 文档提及 G2-B/C，未进 src）——未塞 G2-B/C。未碰组 1 已提交逻辑语义。

---

## 说明（非缺陷，供监督知情，不构成返工）
1. **fp32 源是独立 sidecar `{table}_fp32`，非直接复用 `content_vectors` blob 表。** 调研 §6.A 原话是「fp32 精排向量取自组 1 保留的 content_vectors blob 表」。实现选择在 vec0 同库另起一张 `content_vec_i8_fp32`，由 `make_vector_backend` backfill 时从 `content_vectors` 量化+镜像填入（sqlite.py:862-865 泛化的 `_backfill_vec0`）。等价且更干净（rerank 源与 int8 索引同生命周期、upsert 原子镜像 :369-373，二表不漂移），但确实多存一份 fp32（sidecar）——即 int8 模式磁盘 = int8 索引 + fp32 sidecar + 原 `content_vectors` blob。空间「4× 省」仅指 int8 *索引列* 对 fp32 *索引列*（`test_int8_index_is_smaller_than_fp32` 测的就是这个，且确为精确 4×），非整库省 4×。这与调研「同库、索引 4× 小」表述一致，无误导；若 P1 想进一步省，可让 rerank 直接读 `content_vectors` 省掉 sidecar，属优化非缺陷。
2. `test_int8_two_stage_recall...` 的严格不等 `>` 在我这跑 margin 充足（1.0 vs 0.933），确定种子下稳。

---

## 结论

**PASS。** 六条核查全部独立取证通过：真两阶段（int8 粗召回 vec_int8 包裹→fp32 sidecar 精排）、召回实测两阶段=1.0 且严格 > int8-only=0.933、默认 fp32 安全 opt-in、量化对称/确定/边界正确、复用 Protocol 与共享 helper 且组 1 纯 append 未动、1481 passed/2 skipped + ruff/mypy 绿 + 两召回门仍 1.0、无 usearch/分片越界。一条非阻塞说明（fp32 用独立 sidecar 而非直读 blob 表，等价且更稳，可留 P1 优化）。**无返工。**
