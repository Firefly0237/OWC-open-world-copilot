# P0 设计（编排者补，设计 agent stall；基于 P0_RESEARCH.md 实测结论）

## 0. 架构决策（分层）
把"向量存储/检索"解耦成接口，让两组接力不撞车：

```
VectorRetriever (retrieval/vector.py)
  ├─ holds: backend: VectorSearchBackend
  ├─ search(query, limit)      -> backend.search(qvec, limit) -> 组装 RetrievalHit
  ├─ similarities(query, refs) -> backend.vector_for(ref) 算 cosine（hybrid rerank 用）
  └─ _reindex()                -> 只对变化行 backend.upsert / 删除行 backend.delete（删掉 np.vstack 全量重建）

VectorSearchBackend (retrieval/vector_backend.py, 新)  ← 接口边界
  Protocol: upsert(ref, vec) / delete(ref) / search(qvec, limit)->[(ref,score)] / vector_for(ref)->vec|None / clear()
  ├─ SqliteVecBackend  ← 组 1 落地（fp32, vec0 虚表, 磁盘驻留, 增量, 无损）
  └─ UsearchBackend / int8 量化 / 分层选择  ← 组 2 落地（在同一模块加，不改组 1 的类）
```

**精度决策**：组 1 用 **fp32**（无损，召回与现状逐位等价，验收门 hit_rate 不受影响）。int8 量化（省 4×/快 3×，有损）是性能优化 → 组 2，配"int8 粗召回→fp32 精排候选"的两阶段（fp32 向量仍存在现 content_vectors blob 表里，vector_for 从那取）。**vec0 表本身组 1 存 fp32**；组 2 再决定切 int8 列 + 精排。这样组 1 不引入召回损失、不需调验收门。

## 1. 工作组 1（存储 + 生命周期）— 领地 + 任务
分支 `feature/scale-p0`。**先做完组 1 并合并门禁绿，再开组 2**（接口先稳）。

### 1A 向量后端 sqlite-vec（#1）— retrieval/ + storage/sqlite.py
- **新增 `retrieval/vector_backend.py`**：`VectorSearchBackend` Protocol + `SqliteVecBackend`（fp32）。
  - SQLite 连接里加载扩展：`conn.enable_load_extension(True); sqlite_vec.load(conn)`（封装在 SqliteVecBackend 构造或 storage 连接初始化处；**仅当 sqlite-vec 可用时启用，import 失败要 guided 降级到现 numpy 矩阵后端**——见兼容）。
  - 建虚表：`CREATE VIRTUAL TABLE IF NOT EXISTS {table}_vec USING vec0(ref TEXT PRIMARY KEY, embedding FLOAT[{dim}])`（content_vec / reference_vec 两个，按 vectors_table 参数）。
  - `upsert(ref,vec)` = DELETE ref + INSERT（vec 传 `np.float32(_normalise(v)).tobytes()`）；`delete(ref)`；`search(qvec,limit)` = `SELECT ref,distance FROM {t}_vec WHERE embedding MATCH ? AND k=? ORDER BY distance`（L2，归一化向量下与 cosine 单调等价；score = 转成 higher-better，如 `-distance` 或 `1-distance²/2`）；`vector_for(ref)` 从现 content_vectors blob 表取 fp32（精确）。
- **改 `retrieval/vector.py`**：`__init__` 接受/构造 backend（默认 SqliteVecBackend，回退 numpy）；`search` L113-139 改走 backend.search + 组装 hit（保留 score>0 过滤、limit）；`similarities` L99-111 改 backend.vector_for+dot；`_reindex` L141-212 **删除 L211 `np.vstack`**，把已算出的变化集（L163-173 的 to_embed/changed）→ backend.upsert，删除集 → backend.delete；保留现有 text_hash 增量嵌入逻辑（L165-173）与 blob 表 upsert/prune（精排向量源）。`is_semantic`/`model_id` live property（R4 已修）保持。
- **storage/sqlite.py**：vec0 表的 DDL/连接扩展加载；content_vectors blob 表**保留**（做精排 fp32 源 + 兼容回退）。
- 两 corpus（content + inspiration 参考库 #12）走同一 VectorRetriever，自动都受益。

### 1B 生命周期（#2）— pipeline/ + mcp_server/ + core/skills/ + storage/sqlite.py
- **共享 ProjectContext**（复用既有先例 `service/api.py:_registered_project`）：
  - `mcp_server/tools.py:_project`（L234-246）改为：**若调用方注入了已打开的共享 ctx 则 yield 它（不 open/不 close）；否则保持现自管 open+close**（向后兼容，CLI/单测不变）。handler 体内 `project.xxx` 一字不改。
  - `core/skills/builtin.py:default_skill_registry`（L19-33）：`bind` 增加可选注入共享 ctx 的能力（`partial(tool, project=shared_ctx)` 或 provider）。agent session / multi_agent 一次任务开头 open 一次共享 ctx，结束 close；Diag/Repair/Verifier 共享。
- **replace_* 增量同步**（DELETE 全表+重插 → diff upsert+prune，事务内）：
  - `replace_content_index`（L656-671）：content_index 加 `row_hash`（sha1(title+body)）列；进库前算当前每行 row_hash，与库内 diff → upsert 变化/新增、DELETE 消失行（连带 `DELETE FROM content_fts WHERE ref=?`，普通 fts5 手动删）。
  - `replace_graph_edges`（L816-835）：**先给边造确定性指纹**（`sha1(source|target|kind|edge_type|valid_from|valid_until)` 作稳定 key 或加 UNIQUE）→ diff upsert/delete。
  - `replace_reference_index`（L860-924）：按 source.text_hash 判断哪本书变了，只重切/重插变化 source 的 chunk、prune 删掉的 source；整本不变则跳过（参考库收益最大）。
  - `ContentStore.load`：mtime/size 快路径，未变目录跳过读盘+反序列化（content 文件是 source of truth；content_hash diff 兜正确性，mtime 兜性能）。
- **list_issues 瘦路径**（tools.py L45-66）：纯查 issues 表 → 只连 SQLiteStore(runtime)，不 load bundle/建图/构造 VectorRetriever。边界：若 runtime 库未被任何 full-open 填充则 fallback 到 full open（设计上 list_issues 仅在 session 内库已建好时走瘦路径）。

## 2. 工作组 2（性能 / 搜索加速）— 接口稳定后做
- 在 `vector_backend.py` 加：int8 量化（Python 端 `clip(round(x*127),-127,127).astype(int8)`，确定性封装）+ vec0 INT8 列 + "int8 粗召回(k'=3k)→fp32 精排候选"两阶段；`UsearchBackend`（mmap on-disk ANN）；按 N 阈值/配置选 backend 的策略。
- 规模性能基准（合成随机向量，$0）：5万/30万的内存、KNN 延迟、增量 upsert 延迟；ANN/int8 vs fp32 暴力的 recall@k 对比，验证两阶段 rerank 兜召回。
- **#0 world_id/version 分片**：本程序内做**可行性 spike**（vec0 已验证支持 PARTITION KEY），完整分片（models/schema 加分区维度 + 检索默认 scope）若太大则明确划为 P1 并写清做到哪。

## 3. 兼容 / 迁移 / 红线
- **依赖**：pyproject 加 `sqlite-vec`（pin 版本 + vendored wheel `scratchpad/wheels/`，保证离线可复现安装）。**import 失败要 guided 降级到现 numpy 后端**（不静默崩；老环境仍能跑）——这点让 #1 改造对没装 sqlite-vec 的环境零破坏。
- **迁移**：首次用 vec0 时从 content_vectors blob 表回填（一次性）；feature flag/能力探测控制默认。
- **红线**：真实落地非搭壳、不静默降级、根因修不打补丁、复用既有抽象（blob 表/text_hash 增量/_registered_project 先例）、离线$0、Windows 可跑、**现 1422 tests/ruff/mypy/eval(8 gates)/前端 build 全绿不回归**、检索召回正确性不退化（组 1 fp32 无损即满足）。

## 4. 测试策略
- 组 1：SqliteVecBackend 单测（upsert/delete/search/vector_for、归一化下排序正确、import 失败回退 numpy）；vector.py 改造回归（search/similarities 结果与现 numpy 后端一致——可用同一组向量对拍）；#2 增量同步（改一条对象只 upsert 一行、删对象 prune、graph_edges 指纹 diff、reference 整本不变跳过）；共享 ctx（注入则复用、未注入则自管、写后可见）；list_issues 瘦路径。
- 组 2：规模基准 + recall@k 对拍（见上）。

## 5. 工作分解（有序，给执行对）
**组 1（接力顺序）**：
1. 1A-i：`vector_backend.py`（Protocol + SqliteVecBackend fp32 + 扩展加载 + 回退）。
2. 1A-ii：`vector.py` 接 backend（search/similarities/_reindex 去 vstack）+ storage vec0 DDL + 迁移回填。
3. 1B-i：replace_* 增量（content_index row_hash / graph_edges 指纹 / reference by text_hash）+ ContentStore.load mtime 快路径。
4. 1B-ii：共享 ctx 注入（_project / default_skill_registry 向后兼容）+ list_issues 瘦路径。
- 每步：执行 agent 实现 → 复查 agent 独立 review（根因?打补丁?回归?红线?）→ 迭代过 → 跑领地测试+全门禁。
**组 2**：接口稳定（组 1 合并门禁绿）后启动。

## 6. 待确认/风险（编排者判断：无须阻塞用户的硬岔路，方向已定）
- fp32(组1)/int8(组2) 的精度分层=内部工程选择，已按"组1无损"定，不需用户拍板。
- 完整 #0 分片是否纳入本程序：默认组 2 只做 spike，完整分片划 P1（用户此前未选"连 #0 一起做"那个选项）。若用户想全做再扩。
- 风险（执行须验）：int8 召回损失(组2，rerank 兜)、graph_edges 指纹正确性、FTS5 手动删性能、共享 ctx 并发可见性(reload 触发点=每任务开头跑一次增量 diff)、wheel hash pin。
