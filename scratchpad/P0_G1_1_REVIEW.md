# P0 工作组 1 · #1 (sqlite-vec 向量后端) — 复查 agent 独立 review

分支 `feature/scale-p0`，未提交工作树改动。复查方式：读全 diff + 独立实跑探测 + 全门禁自跑。
对标 `P0_DESIGN.md` §0/§1A/§3/§4。

## 结论：**PASS（带 2 条非阻塞建议）**

7 项核查全部通过。执行 agent 报的数字（1436 passed/2 skipped、ruff/mypy 绿、8 eval gates 绿）
我独立重跑全部复现，未注水。下面逐项给证据，最后列 2 条不影响 PASS 的改进建议（robustness 边角 + 首建轻微冗余）。

---

## 逐项核查

### 1. 真实落地非搭壳 ✅
- 默认路径**真走 SqliteVecBackend**，非偷偷 numpy。实跑：默认 `VectorRetriever(store)` 构造后
  `type(r._backend).__name__ == 'SqliteVecBackend'`（无 backend= 注入 → `_reindex` →
  `_sync_backend` → `store.make_vector_backend` → 真返回 `SqliteVecBackend`）。
- `sqlite_vec_available()` 实返回 `True`，`sqlite_vec` 模块真实装在
  `.venv/Lib/site-packages/sqlite_vec/__init__.py`（pyproject 已 pin `sqlite-vec==0.1.9`）。
- 所有生产调用点都不注入 backend，默认即 sqlite-vec：`service/api.py:1023`、
  `pipeline/project.py:60,98`、`inspiration/retrieval.py:30`（reference 库走 `reference_vec`，
  两 corpus 都受益，符合 §1A 末句）。
- **eval 真走 live backend**：`eval-acceptance` 用默认 `VectorRetriever`（HashingEmbedder，
  无 backend 注入），故召回门是经 SqliteVecBackend 实测出来的，非 numpy。证据见第 6 条门禁输出。

### 2. OOM 目标达成（稳态无常驻全量矩阵）✅
- `vector.py` 里**已无任何 `vstack`/全矩阵驻留**（grep 仅命中 docstring 文字）。唯一 `vstack` 在
  `vector_backend.py:102 NumpyMatrixBackend._rebuild_matrix`，仅回退路径用，符合 §1A。
- `search`（vector.py:131-158）走 `backend.search`，不持有矩阵；`_reindex` 的局部 `vectors` dict
  传入 `_sync_backend` 后即出作用域，sqlite-vec 路径下 `_sync_backend` 只迭代 changed/removed，
  **不留存 vectors**。稳态（两次 reindex 之间）零常驻：search 时按需从磁盘 vec0 读。
- 注：`SqliteVecBackend.search`（vector_backend.py:252）用 `k=count` 拉全表后逐行 `np.frombuffer`
  重算精确 dot —— 这是 search 时**瞬时** O(N)（非常驻），是为 fp32 逐位对拍刻意付的代价，
  N=500 实测正确。见建议 B 对更大规模的说明。

### 3. 根因修非打补丁 ✅
- backend 抽象干净：`VectorSearchBackend` Protocol（upsert/delete/search/vector_for/clear），
  retriever 只跟接口说话。
- **复用既有 blob 表/text_hash 增量**：`_reindex` 保留原 `get_vectors`/`upsert_vectors`/
  `prune_vectors` + text_hash 命中判断（vector.py:195-199），vec0 表首用时从 blob 表**一次性回填**
  (`_backfill_vec0` sqlite.py:751)，blob 表仍是权威 fp32 源（精排/回退用）。没有另起一套。
- **真增量**（实测，给 SqliteVecBackend.upsert/delete 打计数器）：
  - 初次建 5 行：10 upsert（5 回填 + 5 sync）、0 delete
  - 重开**不变** corpus：**0 upsert / 0 delete**（磁盘持久 + backfill 跳过 + text_hash 全命中）
  - 重开 1 改 + 1 删：**正好 1 upsert / 1 delete**
  非"全量包一层"的假增量。

### 4. 不静默降级 ✅
- import / 扩展加载失败 → `SqliteVecBackend._load_extension` 抛 **guided** `SqliteVecError`
  （含安装指引 + 回退说明，vector_backend.py:185-196）；`make_vector_backend` 捕获后
  `logger.info(...)` 输出引导日志并返回 `None`（sqlite.py:740-748），retriever 退到
  `NumpyMatrixBackend`。非静默吞、非裸崩。
- **实跑真失败路径**（monkeypatch 让 `SqliteVecBackend` 构造抛 `SqliteVecError`）：确认打出
  `INFO ... sqlite-vec unavailable for content_vectors (...); using the in-memory numpy vector backend.`
  且 `r._backend` 为 `NumpyMatrixBackend`，`search('caravan')` 仍正常返回命中。回退路径功能真好用。

### 5. 召回正确性 / 对拍 ✅（真对拍，非走过场）
- `test_numpy_and_sqlite_vec_search_are_bit_identical`：同一组 HashingEmbedder 向量，**断言 ref 集合
  + 顺序 + `np.float32` 逐位相等**；语料含 tie 压力（ref:5 近重复、ref:7 与 ref:0 完全重复），
  且 `_populate` 用 reverse-sorted 插入顺序，证明 tie-break 不依赖插入序。
- `test_retriever_search_and_similarities_parity`：跑**整条 VectorRetriever**（embed cache+backend
  sync）两遍——强制 numpy vs 自动 sqlite-vec——断言 search 与 similarities 全等。这正是召回门依赖的属性。
- higher-better + tie-break：`_rank`（vector_backend.py:81）`sort(key=(-score, ref))` 忠实复现旧
  `argsort(-scores, stable)` over `ORDER BY ref` 的"分降序、ties 升 ref"语义；`score>0` 过滤保留在
  vector.py:143。我另跑 N=500 随机向量对 brute-force fp32 排序，ref 顺序逐位一致。
- 14 个测试**全 run 不 skip**（sqlite-vec 已装），非 trivially 通过。

### 6. 无回归 + 红线 ✅（全部自跑复现）
- `pytest tests/ -q` → **1436 passed, 2 skipped**（279.94s）—— 与执行 agent 报数一致。
- `ruff check src/owcopilot/retrieval src/owcopilot/storage tests/` → **All checks passed!**
- `mypy src/owcopilot/retrieval src/owcopilot/storage` → **Success: no issues found in 16 source files**
- `eval-acceptance --workspace <scratch>` → **`"passed": true`**，关键门：
  - `retrieval_hit_rate_gate`: gate 0.9, **hit_rate 1.0**
  - `retrieval_tight_hit_rate_gate`: gate 0.95, **hit_rate 1.0**
  - 8 gates 全绿。召回门经 live sqlite-vec backend 实测达成，fp32 无损不退化（符合 §0 决策）。

### 7. 越界检查 ✅
- 工作树仅动：`pyproject.toml`、`retrieval/vector.py`、`storage/sqlite.py`（改）+
  `retrieval/vector_backend.py`、`tests/test_vector_backend.py`（新）+ scratchpad。
- #2 的三个文件 **完全未动**（`git status --porcelain` 对 `pipeline/project.py`、
  `mcp_server/tools.py`、`core/skills/builtin.py` 均为空）。无越界。

---

## 非阻塞建议（不影响 PASS，建议执行对顺手处理或留组 2）

### 建议 A（robustness，边角崩溃）— `vector_backend.py:237-256` / `sqlite.py:758`
**现象**：磁盘上 vec0 表以某 dim 持久化后，若 retriever 以**不同 dim** 重新构造 backend
（如某配置用 `HashingEmbedder(dim=512)`、或日后换嵌入模型维度变化），`make_vector_backend` →
`_backfill_vec0` → `backend.search(np.zeros(dim))` 会抛**未捕获的 `sqlite3.OperationalError`
("Dimension mismatch ... Expected 1024 ... received 512")**，而非 guided 回退。
我实跑复现（先 dim=1024 建表+upsert 落盘，重开以 dim=512 构造 → 裸崩）。
**为什么算问题**：违反"不静默降级/不裸崩"红线的精神——这是 bare crash 而非 guided。`search` 里
`q.shape[0] != self._dim` 的 dim 守卫拦不住，因为 `self._dim` 是构造维度、不是表里实际维度。
**现实严重度低**：HashingEmbedder 与 bge-m3 默认都是 1024，常规模型切换不触发；要触发需 dim 实际变化。
**建议改法**（任一）：
  1. `make_vector_backend` 的 try 块把 `sqlite3.OperationalError`（或泛 `sqlite3.Error`）一并
     捕获 → 同样 `logger.info` 引导 + 回退 numpy；或
  2. `SqliteVecBackend.search` 把 vec0 抛的 dimension-mismatch `OperationalError` 兜成返回 `[]`
     /转 `SqliteVecError`；或
  3. 构造时校验现存 vec0 表声明维度与 `dim` 一致，不一致则 guided 重建/回退。

### 建议 B（首建轻微冗余 + 大规模 search 内存）— `vector.py:281-284` + `vector_backend.py:252`
- **首建双写**：fresh build 时 `_backfill_vec0` 先从 blob 表 upsert 全部 N 行，紧接着
  `_sync_backend` 又对 `changed_refs`（首建=全 N）再 upsert 一遍 → 同批向量写两次（实测 5 行→10 upsert）。
  结果正确、非全量矩阵，但首建可省一半写入。可在 fresh 路径二选一。
- **search 拉全表**：`search` 用 `k=count` 拉全表重算 dot，瞬时 O(N) 内存。对组 1 的 fp32 无损对拍
  目标是必要代价；但与 MEMORY 里"长线叙事/几十万条/磁盘 ANN"的最终规模目标方向上有张力。
  这正是 **组 2** 该接的（int8 粗召回→fp32 精排、不再全表重算），此处只作记录，**不属 #1 范围**，不返工。

---

## 一句话总结
**PASS** —— 7 项核查全过、门禁数字独立复现属实、真落地真增量真 guided 回退真对拍、未越界；
另附 2 条非阻塞建议（A: dim 变更时的边角裸崩，建议补 guided 兜底；B: 首建双写/大规模 search 全表，留组 2）。
