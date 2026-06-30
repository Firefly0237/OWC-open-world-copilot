# P0 工作组 2（性能）调研报告 — 编排者实测（调研 agent 进程退出，编排者接手跑探针）

**全部本机实测**（隔离 `scratchpad/testvenv`，usearch 2.25.3 cp313；项目 venv 未污染）。探针：probe_usearch_e2e.py / probe_recall.py / probe_recall_clustered.py / probe_usearch_expansion.py。

## 0. 结论先行
1. **int8（sqlite-vec INT8 列）+ 两阶段 fp32 精排 = recall ~1.0**——可靠、无召回损失风险、$0、同库。**这是组 2 的稳妥核心。**
2. **usearch ANN 可用于亚线性加速，但必须调参**（默认参数是陷阱）；配两阶段精排可到 ~0.99 recall。on-disk mmap 可用（内存受限友好）。**作为大 N tier，需显式调参 + 两阶段。**
3. **#0 world_id/version 分片**=正交的"降 N"杠杆（sqlite-vec PARTITION KEY 已验证支持），是让暴力/int8 也够快的根本手段。

## 1. usearch 端到端（实测通过）
- import / `Index(ndim=1024, metric='cos')` OK；**默认 dtype=BF16**（非 f32，注意）。
- add / search（自检索 rank0 命中）/ `get(key)`→取回向量（cos 0.9999）/ `remove`→contains False：全 OK。增量 add/remove 支持。
- **save→磁盘 + `view()`/`restore(view=True)` = mmap on-disk**（不全进内存）OK：2000×1024 BF16=4.4MB；search-after-view 正确。→ 内存受限下 ANN 可行。
- dtype：bf16（近无损，自检索命中）/ f16（4.4MB）/ i8（2.35MB，但 ANN 默认参下召回差）。

## 2. int8 两阶段精排召回（N=30000，recall@10）
| 路径 | iid 随机 | 聚类(真实更像) |
|---|---|---|
| int8 暴力（== sqlite-vec INT8 列）单独 | 0.8405 | 0.8255 |
| **int8 粗召回 3k → fp32 精排 top-k** | **0.9990** | **0.9975** |
| int8 粗召回 5k → fp32 精排 | 1.0000 | — |
→ **两阶段把 int8 的召回损失完全补回 ~1.0**。设计的"int8 粗召回→fp32 精排"成立。fp32 精排向量取自组 1 保留的 content_vectors blob 表。

## 3. usearch ANN 召回——默认参数是陷阱（关键教训）
聚类数据 N=30000 recall@10：
| 配置 | recall@10 | 延迟 | build |
|---|---|---|---|
| 默认 conn=16/exp_search=64 | 0.43（甚至更低，旧探针出现 0.12） | 0.11ms/q | 4.4s |
| conn=16/exp_search=256 | 0.54 | 0.59ms/q | 6.4s |
| **conn=32/exp_add=200/exp_search=512** | **0.9035** | **0.86ms/q** | 16.4s |
- usearch `exact=True`（暴力，非 ANN）：recall 1.0 但 17ms/q（不比 sqlite-vec 快，无意义）。
- 结论：**usearch HNSW 必须调 connectivity≥32 + expansion_search≥512 才到 0.90**；默认参会给出误导性的差召回（0.12–0.43）。配两阶段精排（ANN 粗召回 3k→fp32 精排）可把 recall 推到 ~0.99。延迟 ~1ms/q = 比 30k 暴力(int8 ~80ms/q、fp32 ~420ms/q)快约 100×，**这才是亚线性搜索的真加速**。

## 4. 规模与内存
- sqlite-vec（#1 已测）：30万 fp32 ~2.5s/q、int8 ~0.8s/q（磁盘暴力 O(N)）；int8 文件 4× 小。
- usearch ANN（调参后）：~1ms/q（亚线性），on-disk view mmap 不全进内存，索引文件 BF16 约为 fp32 一半。
- → 大 N（几万→几十万）下 usearch ANN 的速度优势压倒性；但要付：独立索引文件 + 调参 + 召回需两阶段兜。

## 5. 与组 1 接口契合 + 风险
- `UsearchBackend` 实现 `VectorSearchBackend`：`upsert`=add(key,vec)/`delete`=remove(key)/`search`=search(q,k)→[(ref,score)]/`vector_for`=get(key) 或从 blob 表取 fp32。key↔ref 需映射（usearch 用 uint64 key；ref 是字符串 → 维护 ref↔int 映射表，或用稳定 hash）。
- **风险（必须设计处理）**：
  1. usearch 索引是**独立 .usearch 文件**，不在 SQLite 事务里 → 增量 add/remove 后与 content_vectors blob/content_index 的一致性、崩溃恢复（写到一半进程死）要有策略（如：blob 表是真值，usearch 可重建；启动校验 count 不一致则重建）。
  2. **调参是硬约束**（默认参=召回陷阱）——必须在代码里固定 conn=32/exp_search≥512 并测召回门，不能用默认。
  3. dim/model 变更 → usearch 索引重建（同 sqlite-vec）。
  4. ref↔uint64 key 映射的持久化与稳定性。

## 6. 组 2 推荐落地（给设计）
- **A. int8 + 两阶段精排（sqlite-vec）**：在 `SqliteVecBackend` 加 int8 vec0 列选项 + "int8 粗召回 k'=3k→从 blob 表取 fp32 精排 top-k"。无召回损失（~1.0）、4× 省空间、3× 快扫描、同库。**优先做、最稳。**
- **B. UsearchBackend（大 N tier）**：实现 `VectorSearchBackend`，**固定调参 conn=32/exp_add=200/exp_search=512**，on-disk view，配两阶段精排，按 N 阈值（如 ≥5万）从 sqlite-vec 暴力切到 usearch ANN。blob 表为真值、usearch 可重建以解一致性。
- **C. #0 分片 spike**：sqlite-vec PARTITION KEY 按 world_id/version 把每查询 N 降到当前版本量级（正交于 A/B，往往让暴力/int8 就够快）。完整分片（models/schema 加维度）大、可只做 spike，留 P1。
- 测试：合成向量的 recall@k 门（int8 两阶段 ≥0.99、usearch 调参后 ≥0.95 配两阶段）、规模延迟基准、一致性/崩溃重建测试。$0 合成即可。
