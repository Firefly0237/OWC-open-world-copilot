# P0 #2a 增量同步 — 复查报告（工作组 1 / 复查 agent）

复查对象：未提交工作树改动（分支 `feature/scale-p0`，基线 = 已提交 #1 `515eb74`）。
范围：`src/owcopilot/storage/sqlite.py`、`src/owcopilot/content/store.py`、新增 `tests/test_sqlite_incremental_sync.py`。
方法：全部独立核查 + 实跑（25 个新测试 + 5 个我自构 probe + 全量 1462 测试 + ruff + mypy + eval-golden + eval-acceptance）。

---

## 裁决：**PASS**（0 阻断项；2 条非阻断观察，见末尾）

---

## 1. parity 真实性 — PASS

- 测试是**真逐位对拍**：同一 target bundle 下，full 路径（空库直接建到 target）vs incremental 路径（先建一个**不同** bundle 再 `replace_*` 到 target），断言 `content_index`（含 row_hash 全字段）、`content_fts`、`graph_edges`、reference 三表的**行集合 + 全字段**一致，并断言 FTS 可查性（`search_content`）。
  - content_index 对拍：`tests/test_sqlite_incremental_sync.py:168-184`，快照 helper 取全字段含 row_hash（`:96-103`）、fts 全字段（`:106-112`）。
  - graph_edges 对拍 + 行数（含重数）一致：`:272-293`。
  - reference 三表对拍：`:365-401`，快照 `_reference_rows` 覆盖 sources/chunks/fts 全字段（`:126-147`）。
- 我对照了 #1 已提交的旧 drop-and-reinsert 实现（`git show 515eb74` — content_index `DELETE+executemany`、graph_edges 同、reference 三 DELETE+三 executemany），确认 parity 断言的“全量”就是旧真值。
- 实跑：`pytest tests/test_sqlite_incremental_sync.py -q` → **25 passed**。
- **执行 agent 未覆盖的 case，我自构实跑全部 PASS**（probe.py）：
  1. `A,B,C → B + 改过的 C + 新增 D` 对拍全量：content_index/fts 行集合一致；A 不再可查、D 可查、C 在新文本下可查且**旧文本 'orig C' 不再可查**（无陈旧 fts）；`fts_refs == ci_refs`（无孤儿、无漏）。**PASS**
  2. 平行边 3→2（删掉一条重复边）对拍全量：relation 边数 = 2，指纹唯一。**PASS**
  3. 平行边 1→3（增重复）对拍全量。**PASS**

## 2. 不静默丢数据 / 不过度 prune — PASS

- content_index：removed = `existing_hash` 中不在 `desired` 的 ref（`sqlite.py:714`），只删真正消失的；删 content_index 行**同时**删 content_fts 行（`:723-725`）。changed 行先 `DELETE FROM content_fts WHERE ref=?` 再重插（`:742-746`），普通 fts5 无 upsert 时这是正确手法，不留孤儿、不漏删。我的 probe1 实测 `ci_refs == fts_refs` 恒成立。
- reference：prune 走 `_delete_reference_source`（`:1123-1134`），连带删该 source 的 chunks（按 source_id）+ 每个 chunk 的 fts 行（plain fts5 手删）+ source 行。改过的 source 也先整体删再重插（`:1069`），避免上一版更长的 chunk 残留。`test_reference_index_edited_book_rechunks_only_that_source`（`:433-471`）+ 我的 drop_one/edit_one probe 验证陈旧 chunk + 其 fts 行均消失。
- 不过度 prune：`test_reference_index_unchanged_book_zero_writes`（`:404-430`）用 `set_trace_callback` 统计写语句 = 0，证明未变的书一行都不动。

## 3. graph_edges 指纹正确性 — PASS

- 指纹 = `sha1(source|target|kind|edge_type|valid_from|valid_until|#occurrence)`（`_edge_fingerprint` `sqlite.py:1471-1487`），occurrence ordinal 由 `replace_graph_edges` 内 `seen` 计数器逐 key 递增（`:953-967`）。同一 (src,target,kind,edge_type,valid) 的 N 条平行边 → `#0..#(N-1)` → N 个唯一指纹 → N 行，不塌缩。
- UNIQUE 约束 `idx_graph_edges_fingerprint`（`:271-274`）与 diff 自洽：diff 只 INSERT `added = desired - existing` 的新指纹，已存在的跳过；occurrence 保证每条平行边指纹不同，故 INSERT 不会撞 UNIQUE。
- 实跑：`test_graph_edges_preserves_duplicate_multiplicity`（`:296-326`，含 2 条 knows 平行边）+ 我的 probe2（含 valid_from/until 的平行边，3→2 删一条）+ probe3（1→3 增）全部 PASS，行数与 full rebuild 逐一致。

## 4. mtime 缓存正确性 — PASS（有一条已知理论风险，非阻断）

- `save()` 清缓存：`store.py:78` `self._parse_cache.clear()`，注释说明动机。`test_content_store_save_invalidates_cache`（test:517-530）实测 `_parse_cache == {}`，save 后 load 读到新值。
- 同进程改文件 → mtime_ns 变 → cache miss 重读：`_read_parsed`（`store.py:96-114`）键 = `(mtime_ns, size)`，任一变即 miss。`test_content_store_load_reparses_changed_file`（test:500-514）实测改一文件只多 1 次 miss，其余命中，读到新值。
- 缓存只读路径用、不影响写正确性：缓存仅在 `_load_*` 读路径命中；写路径不读缓存，且 save 先 clear。**正确**。
- **理论陈旧风险（已评估，低危，非阻断）**：若**外部进程**改文件且 (mtime_ns,size) 恰好都不变，则同一 store 实例会命中旧缓存。我用 `os.utime(..., ns=...)` 强制制造碰撞（probe5）**复现了陈旧读**。严重度评估为**低**：
  - 需要纳秒级 mtime 完全不变（NTFS/现代 fs 纳秒精度下，正常写入几乎必然改变 mtime_ns）**且** size 字节不变（同长度替换）**且**为外部进程改动（同 store 的 save 已 clear）。三条同时成立属构造性极端。
  - 设计注释（`store.py:96-104`、`:73-77`）已明确承认这是性能快路径、files 为真值，并接受此权衡；新建 store 永远读盘真值（probe5 验证 fresh store 读到 v2）。
  - 与 P0_DESIGN §1B“content_hash diff 兜正确性，mtime 兜性能”的分层一致——真正的正确性兜底在上层 content_hash，本层只做性能。结论：可接受，无需返工。

## 5. 迁移安全 — PASS

- `graph_edges` 加 `edge_fingerprint` 列时 `DELETE FROM graph_edges`（`sqlite.py:259-267`）：graph_edges 完全可从 bundle 重建（`replace_graph_edges` 每次 open/reload 全量 diff，调用点 `pipeline/project.py:51,94`、`service/api.py:1020`），无独立真值丢失。理由正确——旧行全带 `''` 默认会撞新建的 UNIQUE 索引，清掉比回填更省且无损。
- `content_index` 加 `row_hash DEFAULT ''` 在位升级（`:258`）：旧行保留、row_hash=''。`replace_content_index` 把 `existing_hash.get(ref) != h` 判为 changed（`:718-720`），`''` 永不等于真 sha1，故首次 resync 必重插并回填真 hash。
- **我实跑了完整迁移 probe4**：手建一个**无 row_hash / 无 edge_fingerprint** 的旧库（含 1 content_index 行 + 1 fts 行 + 1 graph_edges 行），用 `SQLiteStore(path)` 打开触发 `initialize()` 迁移：
  - content_index 旧行存活且 row_hash=''（未丢、未误标）；
  - graph_edges 被清空（=0，避免 UNIQUE('') 碰撞）；
  - 首次 `replace_content_index` 后旧行 row_hash 被重新打成真 hash，且与 full rebuild 逐位一致；graph 重建成功。**PASS，无数据丢失。**
- UNIQUE 索引用 `IF NOT EXISTS` + 列默认在 DELETE 之后建（`:271`），顺序正确（先清掉 '' 行再建唯一索引，不会建索引时报冲突）。

## 6. 无回归 + 红线 — PASS

- 全量测试：`pytest tests/ -q` → **1462 passed, 2 skipped**（300.78s）— 与声明完全一致。
- `ruff check src/owcopilot/storage src/owcopilot/content tests/` → All checks passed。
- `mypy src/owcopilot/storage src/owcopilot/content` → Success: no issues found in 22 source files。
- `eval-golden`（fresh workspace）→ **passed=True**，5 检查全绿（audit/retrieval/qa/export/provenance）。
- `eval-acceptance`（fresh workspace）→ **passed=True**，8 gate 全绿，含 `retrieval_hit_rate_gate` + `retrieval_tight_hit_rate_gate` + `seeded_error_detection_gate`——证明审计/检索结果未因增量同步变样（fp32 无损 + 行集合 parity 的预期结果）。
- 事务红线：三个 replace_* 均 `try: …executes…; except: rollback(); raise` + 末尾单 `commit()`；连接未设 `isolation_level=None`（无 autocommit，已 grep 确认），故 DML 在隐式事务内、单次 commit 原子、异常回滚整批。“单事务”声明成立。

## 7. 无越界 — PASS

- `git diff --stat`：仅 `content/store.py`、`storage/sqlite.py` 改动 + 新增测试文件。
- 2b 文件未碰：`git diff --name-only -- src/owcopilot/pipeline src/owcopilot/mcp_server/tools.py src/owcopilot/core/skills/builtin.py` → 空。
- #1 已提交 surface 未碰：`git diff --name-only -- retrieval/vector_backend.py retrieval/vector.py` → 空；sqlite.py 的 diff hunk 仅落在 schema/迁移块、三个 replace_*、三个新 helper（`make_vector_backend`/`_backfill_vec0` 等 #1 函数不在 hunk 内）。

---

## 非阻断观察（不要求返工，记录备查）

- **OBS-1（低危，已接受）** `store.py:96-114` 的 mtime/size 快路径在“外部进程 + 纳秒 mtime 不变 + size 不变”的构造性碰撞下可读到陈旧值（probe5 强制 `os.utime` 已复现）。设计已显式接受此权衡且上层 content_hash 兜正确性；无需改。若日后想彻底消除，可在键里再加一个内容字节的弱校验（如前 N 字节）或对外部修改场景每次 stat 后比对 content_hash——属优化，非缺陷。
- **OBS-2（无影响）** `replace_content_index` 新路径用 `desired[ref]` dict 去重（`sqlite.py:703-707`），旧 `executemany` 路径遇同 ref 会因 PRIMARY KEY 抛错。实际 `_content_rows` 的 ref 对每类对象都唯一（relation 含 index、其余按 dict id），不存在真实碰撞，行为无差异；仅记录该细微语义差。

---

**结论：PASS。**
