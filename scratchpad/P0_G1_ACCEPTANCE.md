# P0 工作组 1 · 整体验收报告（组 1 整体验收 agent，只读）

**验收对象**：分支 `feature/scale-p0`，三提交累计
- `515eb74` #1 — sqlite-vec 磁盘驻留向量后端（fp32, 无损）
- `3ff87ee` #2a — 增量 `replace_*` 同步 + parse-cache 快路径
- `059ce92` #2b — 共享 ProjectContext 注入 + list_issues 瘦路径

**基线**：master `a231a36`。HEAD=`059ce92`，工作树干净（仅 `scratchpad/` 未跟踪）。
**方法**：读全 diff（`git diff master..feature/scale-p0 -- src/ pyproject.toml`，8 文件 +988/-206）+ 读三份复查 + **全门禁独立实跑** + **5 段自构 scale 探针实跑**（不依赖执行/复查 agent 的测试）。

---

## 裁决：**ACCEPT**（0 FAIL 项）

设计 §1 兑现、全门禁独立实跑真绿、SCALE_AUDIT 两个 🔴 在「当前实现层面」真被解决（组 1 该解的部分），未越界、无静默降级。

---

## A. 设计 §1 兑现 + 全门禁真绿（逐一实跑）

### A1 门禁实跑结果（全部本机独立复现，非信报告）

| 门禁 | 实跑命令 | 结果 |
|---|---|---|
| 单测 | `pytest tests/ -q` | **1468 passed, 2 skipped**（278.52s）✅ 与目标逐字相符 |
| lint | `ruff check src/ tests/` | **All checks passed!**（exit 0）✅ |
| 类型 | `mypy src/` | **Success: no issues found in 231 source files** ✅ |
| 验收门 | `eval-acceptance` | 顶层 `passed: true`，**8 gates 全绿** ✅ |
| 黄金集 | `eval-golden` | 顶层 `passed: True`，**5 检查全绿** ✅ |

eval-acceptance 8 gate 实测值（经 live sqlite-vec backend，非 numpy 回退）：
- `retrieval_hit_rate_gate`: gate 0.9 → **hit_rate 1.0**
- `retrieval_tight_hit_rate_gate`: gate 0.95 → **hit_rate 1.0**
- `seeded_error_detection_gate`: gate 0.85 → **1.0**（25/25 检出）
- `tool_selection_accuracy_gate`: gate 0.8 → **mean_f1 1.0**
- `impact_recall_100` / `clean_world_zero_false_positives` / `qa_citation_existence_or_refuse` / `qa_faithfulness_entailment`（$0 离线 skip，fail-closed）全过。

**fp32 无损结论成立**：召回门 1.0 是经 SqliteVecBackend 实测出来的，证明组 1 的向量后端切换未引入召回退化（符合设计 §0「组 1 无损」决策）。

### A2 设计 §1 territory 兑现（diff 全貌核对）
diff 落在设计指定的 8 个文件，量级与 territory 一致：
- 1A：`retrieval/vector_backend.py`（新，274 行）+ `retrieval/vector.py`（接 backend、去 vstack）+ `storage/sqlite.py`（vec0 DDL/迁移/backfill）+ `pyproject.toml`（pin `sqlite-vec==0.1.9` + 注释）。
- 1B：`storage/sqlite.py`（三个 `replace_*` 增量化）+ `content/store.py`（mtime 快路径）+ `mcp_server/tools.py`（`_project` shared 分支 + `_issues_store` 瘦路径）+ `core/skills/builtin.py`（`default_skill_registry` 注入 `project=`）+ `cli/main.py`（agent/multi_agent 接线）。
- 依赖：`scratchpad/wheels/sqlite_vec-0.1.9-py3-none-win_amd64.whl` 已 vendored（离线可复现）。

---

## B. 规模目标真达成（实证，整个程序的目的）

> 全部为本验收 agent **自构探针实跑**，独立于执行/复查 agent 的测试。脚本见 `scratchpad/probe_*_g1acc.py`。

### #1 内存：默认不再把整库堆成常驻 numpy 矩阵 — **PASS**
- `sqlite_vec_available()` → **True**；默认 `VectorRetriever(store)` 构造后 `type(r._backend).__name__ == 'SqliteVecBackend'`（**live，非回退**）。
- `vector.py` 全文**无 `vstack`/全矩阵驻留**（唯一 vstack 在 `vector_backend.py:102 NumpyMatrixBackend._rebuild_matrix`，仅回退路径）。retriever 实例**无 `_matrix` 属性**（探针 `hasattr(r,'_matrix')==False`）。
- `search` 走 `backend.search`（磁盘 vec0）；实测 40 向量驻留在磁盘 `content_vec` 虚表，search 返回 5 hits 正常。稳态无常驻全量矩阵。

### #1 重建：改一行 = 增量 upsert（非全量重建）— **PASS**
- 改 1 行 body → `_reindex` 实测 **upsert=1, delete=0, 26.4ms**（含小语料嵌入），非全量。
- 重开**不变**语料 → **upsert=0, delete=0**（磁盘持久 + backfill 跳过 + text_hash 全命中）。
  > 设计说「17ms 级」；实测 26ms 是 N=40 小语料含一次嵌入开销的量级，性质=增量单行（不随库规模线性增长），与设计意图一致。

### #2a 增量：三个 `replace_*` 真 diff（不再 DELETE 全表）+ parity — **PASS**
- 源码核对：`replace_content_index`（按 ref + row_hash diff，upsert changed / prune removed）、`replace_graph_edges`（按 `sha1(source|target|kind|edge_type|valid_from|valid_until|#occurrence)` 指纹 diff，UNIQUE 索引）、`replace_reference_index`（按 source.text_hash 整本跳过/重切）。runtime 路径**无 `DELETE FROM <全表>`**——唯一全表 DELETE 在 `sqlite.py:267`，是 `edge_fingerprint` 列**一次性迁移**（图可从 bundle 重建，正确）。
- **parity 实测**：先给库种入一条 bogus content_index/fts 行，再 incremental `replace_*` 同步到 target → content_index/content_fts/graph_edges **行集合（全字段含 row_hash/fingerprint）与全量重建逐位一致**，bogus 行被正确 prune。

### #2b 共享 ctx：一次任务复用同一 ctx（只 open 一次）— **PASS**
- **实测开计数**：owner `open()` 一次 → 同一 registry 连跑 audit/list_issues/build_context_pack/impact_of/quality_harness **5 个工具** → `ProjectContext.open` 计数**恒为 1**，close=1。证明 5 次工具调用全复用注入 ctx，无一重开。
- CLI 接线确认：`_cmd_agent`/`_cmd_multi_agent` 各 `open` 一次 + `try/finally: project.close()`；multi_agent 的 Diag/Repair/Verifier 经同一 registry（已绑 shared project）→ 同一 ctx。

### #2b list_issues 瘦路径：不构造 bundle/graph/vector — **PASS**
- **绊线实测**（patch `ProjectContext.open` / `build_content_graph` / `VectorRetriever` 三处当 tripwire）：`list_issues(project=None)` 跑完 **heavy==[]**——三类重构造一个都没触发，只连 `SQLiteStore` 读 `issues` 表。空/未填充库返回 `count=0` 不报错。

### 红线核查（无静默降级 / 无裸崩）— **PASS**
- **sqlite-vec 整体不可用**（探针让构造抛 `SqliteVecError`）→ guided INFO 日志 + 退 `NumpyMatrixBackend`，search 仍正常。
- **dim 不匹配**（先 dim=1024 落盘，再 dim=512 构造）→ `make_vector_backend` 捕获 `sqlite3.OperationalError`，guided INFO 日志 + 退 numpy，search 仍正常。
  > 注：这正是复查 #1「建议 A（dim 变更裸崩）」指出的边角；本验收实测**已被修复**（`make_vector_backend` 的 except 同时捕获 `SqliteVecError` 和 `sqlite3.OperationalError`，`vector_backend.py` 文档亦载明）。

---

## C. 规模目标：解了什么 / 留给组 2 什么（诚实结论）

### SCALE_AUDIT 两个 🔴 在「当前实现层面」的状态

**#1（RAG 全进内存矩阵 OOM）— 组 1 范围内解决：**
- ✅ 解了「常驻全内存矩阵」：稳态零常驻矩阵，向量磁盘驻留（vec0），改一行=单行 upsert，重开不变库=0 写。OOM 的**常驻**根因（每开一个项目吃 1–2 GB 矩阵）已消除。
- ⚠️ **未解（明确划给组 2，非组 1 未达标）**：`SqliteVecBackend.search` 仍 `k=count` 拉全表逐行重算 dot = **search 时瞬时 O(N) 暴力扫描**（这是为 fp32 逐位对拍刻意付的代价，使召回门不需调）。真 ANN（IVF/HNSW-on-disk）、int8 量化（省 4×）、「int8 粗召回→fp32 精排」两阶段 = **组 2**。即组 1 解了**内存常驻**与**重建成本**，未解**单次 search 的 CPU 复杂度**——后者是组 2 的领地，接口（`VectorSearchBackend` Protocol）已为其留好边界，加新 backend 不动现有类。

**#2（每工具/每 step 全量重开项目 = 放大器）— 组 1 范围内解决：**
- ✅ 解了「每调重开」：session 级共享单 ctx（实测一次任务 open=1），多 agent worker 共享。
- ✅ 解了「全量重建」：三个 `replace_*` 从 DELETE 全表+重插 → 真 diff（upsert changed + prune removed），parity 逐位一致；list_issues 走瘦路径不付 load/graph/vector 开销。
- 即 SCALE_AUDIT 标注的「全量重建」一并被拔掉。

### 明确不属组 1、未碰（与设计一致，非缺陷）
- #0 world_id/version 分片（设计划组 2 spike / P1）；#3 FTS5 CJK 分词器；#4 增量审计；#5 社区检测增量 + relay 度数上限；#6 relation 索引；#11 大规模评测集。这些 SCALE_AUDIT 列为 🟠/🟢 或更后优先级，设计 §1 未纳入组 1，本验收**不计为组 1 未达标**。

### 三份复查的 follow-up 当前状态
- 复查#1「建议 A（dim 裸崩）」→ **本验收实测已修**（见 B 红线）。
- 复查#1「建议 B（首建双写 / 大规模 search 全表）」→ 全表 search 明确属组 2；首建双写为轻微冗余，结果正确、非全量矩阵，不返工。
- 复查#2a「OBS-1（外部进程纳秒 mtime+size 双不变的构造性陈旧读）」→ 设计已显式接受（上层 content_hash 兜正确性），低危，不阻断。
- 复查#2b「F-1（MCP transport 真 FastMCP schema 对新 `project` 参未被测试覆盖）」→ **唯一仍开放的预防性 follow-up**：对当前 $0 离线 / CLI agent 路径**零影响**（全门禁绿即证），仅在「有人装 `[mcp]` extra 跑真 FastMCP transport」时才可能触发；本仓未装 `mcp` SDK 无法在 CI 实测。**不阻断组 1 ACCEPT**，建议组 2/后续按方案 2（注入脱离签名，如 contextvar）顺手加固。

---

## 最后一句话

**ACCEPT** —— 设计 §1 全兑现、5 门禁独立实跑真绿（1468 passed / ruff / mypy / 8 acceptance gates / 5 golden checks）、SCALE_AUDIT 两个 🔴（#1 内存常驻矩阵 + 全量重建、#2 每调重开放大器）在组 1 该解的层面经自构探针实证真被解决，未越界、无静默降级、无裸崩；无 FAIL 项（唯一开放项 F-1 仅影响未启用的真 MCP transport 路径，非当前路径，不阻断）。
