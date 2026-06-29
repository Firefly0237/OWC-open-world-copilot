# P0 调研报告（实测，非综述）

调研 agent 产出。**只读 + 隔离探测**，未改任何项目代码、未污染项目 venv。
所有结论来自本机本项目实跑，命令与原始输出可复现（见文末「探测脚本与证据」）。

环境事实（实测 `F:\openworld\.venv\Scripts\python.exe`）：
- Python **3.13.12**（Anaconda, MSC v.1942, 64-bit）
- `sqlite3.sqlite_version` = **3.51.1**
- `hasattr(sqlite3.Connection, 'enable_load_extension')` = **True**
- 网络可用（pip 能从 PyPI 取 wheel；并非真air-gap，"离线$0"是运行时约束，不是安装期约束——见风险①）

---

## 0. 一句话结论（先看这个）

**sqlite-vec 在本机能用——这是决定性发现。** 本机 Python 的 `enable_load_extension` 没被禁用（Windows 上最常见的出局原因在这里不成立），`sqlite-vec` 的 win_amd64 wheel **自带 `vec0.dll`**，离线 `--no-index` 可装，load 成扩展后 KNN / int8 / 增量 upsert / delete / 元数据列过滤 / **PARTITION KEY** 全部实跑通过。

**推荐后端：sqlite-vec（首选）**，理由是与现仓库"SQLite-everything + FTS5 同库"架构零摩擦、磁盘驻留、单行增量 upsert 17ms、int8 省 4× 空间且 KNN 快 3×。

**但必须对设计 agent 钉死一个反直觉事实：sqlite-vec 0.1.9 的 `vec0` 是暴力线性扫描（brute-force），不是 ANN。** `EXPLAIN QUERY PLAN` 实测为 `SCAN vec VIRTUAL TABLE`。它解决的是**内存常驻 + 全量重建**问题（#1 的真痛点），不解决**搜索 O(N)** 问题。好在 #1 现状本来就是暴力 matmul（也是 O(N)），所以这不是退化；且红线允许用「两阶段 rerank 兜 ANN 召回」——而 sqlite-vec 根本无召回损失（精确）。若未来 N 到几十万要真 ANN，那是 P1 分片维度（#0 world_id/version）该解的事，或届时切 usearch。

---

## 生死项 1：sqlite-vec 可行性（实测全过）

| 探测 | 结果 |
|---|---|
| `enable_load_extension(True)`（项目 venv，含 WAL+busy_timeout 的真实文件库） | **OK**，无 AttributeError/OperationalError |
| `pip download sqlite-vec --no-deps` | 取到 `sqlite_vec-0.1.9-py3-none-win_amd64.whl`（292 KB），**纯 Python wheel，零依赖** |
| wheel 内容 | `sqlite_vec/__init__.py` + **`sqlite_vec/vec0.dll`**（原生扩展随 wheel 提供，无需编译） |
| 离线安装 `pip install --no-index --find-links <wheels>` | **成功**（隔离 testvenv，未碰项目 venv） |
| `import sqlite_vec; sqlite_vec.load(db)` + `vec_version()` | **`v0.1.9`** |
| float32 KNN（`embedding MATCH ? AND k=3 ORDER BY distance`） | **OK**，距离排序正确 |
| 增量 upsert（DELETE 旧 ref + INSERT 新向量） | **OK**，重排后该 ref 正确移到远端 |
| delete / prune（`DELETE FROM v WHERE ref=?`） | **OK**，count 正确递减 |
| int8 KNN（`vec_int8(?)` 包裹 + `INT8[dim]` 列） | **OK**（注意：必须用 `vec_int8()` 构造器或声明类型，裸 blob 会报 "expected int8 got float32"） |
| 元数据列 + 过滤（`... AND world = ?`） | **OK** |
| **PARTITION KEY**（`world TEXT PARTITION KEY`） | **OK**——直接服务未来 #0 world_id/version 分片，等于内置 scope 维度 |
| `vec_quantize_i8` SQL 函数 | **不可用**（0.1.9 无此函数）→ 量化须在 Python 端做（numpy `clip(round(x*127))`，一行，已实测） |

### KNN API 形态（给设计 agent 抄）
建表：`CREATE VIRTUAL TABLE content_vec USING vec0(ref TEXT PRIMARY KEY, embedding FLOAT[1024])`
写入：`INSERT INTO content_vec(ref, embedding) VALUES (?, ?)`，向量传 `np.float32` 的 `.tobytes()`
查询：
```sql
SELECT ref, distance FROM content_vec
WHERE embedding MATCH ? AND k = ? ORDER BY distance
```
`distance` 默认 L2；对单位归一化向量，L2 与 cosine 单调等价（现 `_normalise` 已做归一化，可直接复用）。
增量：改一行 = `DELETE FROM content_vec WHERE ref=?` + `INSERT ...`（无全量重建）。
删除：`DELETE FROM content_vec WHERE ref=?`。

### 实测规模数据（50k × 1024-dim，本机笔记本，scratchpad 临时库）
| 指标 | fp32 | int8（量化） |
|---|---|---|
| 插入 50k 行耗时 | 58.3 s | **1.9 s**（30× 快，因 blob 小） |
| 库文件体积 | 208 MB | **54 MB**（≈4× 小） |
| KNN k=10 中位延迟 | 422 ms | **137 ms**（≈3× 快） |
| 单行 upsert+commit | — | **16.7 ms**（无全量重建，这是 #1 的核心胜点） |
| 冷开库 + KNN | 377 ms（无需把矩阵堆进内存） | — |
| `EXPLAIN QUERY PLAN` | `SCAN vec VIRTUAL TABLE`（**确认暴力扫描**） | 同 |

外推：30 万 × 1024 fp32 KNN ≈ 6×422 ≈ 2.5 s/query；int8 ≈ 6×137 ≈ 0.8 s/query。**int8 是必须上的**（既省内存又压住搜索成本）。这正是红线"两阶段 rerank"能兜的场景：int8 粗召回 → 取回原始/更高精度向量精排小候选（不过 sqlite-vec 本身精确，主要靠 int8 量化损失需 rerank 补，而项目已有两阶段 rerank 链路）。

---

## 生死项 2：备选后端（逐个实测离线可装 + 本机可跑）

| 后端 | 离线可装? | 本机 import/跑? | 内存特性 | 量化 | 增量 | Windows | SQLite 契合 | 推荐度 |
|---|---|---|---|---|---|---|---|---|
| **sqlite-vec 0.1.9** | ✅ wheel 自带 vec0.dll，`--no-index` 实测装成 | ✅ **全流程实测通过** | **磁盘驻留**，查询不堆 Python 矩阵 | int8（Python 端量化） | ✅ 单行 upsert 17ms | ✅ 原生 | **满分**：同库同事务，FTS5+向量一处 | **首选** |
| **usearch 2.25.3** | ✅ `usearch-2.25.3-cp313-cp313-win_amd64.whl` 实测 download 成 | 未在 testvenv 跑端到端（wheel 在手，import 风险低；cp313 原生 wheel 存在） | **mmap on-disk**（真 ANN，HNSW 但可 view/mmap 不全进内存） | ✅ int8/f16 原生 | ✅ add/remove | ✅ 原生 cp313 wheel | 中：另一存储，要自己和 SQLite 协调一致性 | **次选**（若未来要真 ANN/几十万级） |
| **faiss-cpu 1.14.3** | ✅ `faiss_cpu-1.14.3-cp313-cp313-win_amd64.whl`（16 MB）实测 download 成 | 未跑端到端 | IVF/Flat 可落盘但常用法是内存索引；HNSW 常驻内存 | ✅ PQ | 弱（IVF 重训不友好增量） | ✅ 有 cp313 wheel | 低：重、与 SQLite 两套 | 不推荐（重，增量差，审计内存约束） |
| **LanceDB** | 未 download（依赖较重：pyarrow 等；按需再验） | — | 列式磁盘 + mmap，IVF-PQ | ✅ PQ | ✅ | 一般 | 低：独立列式存储与 SQLite 并存 | 备选（仅当参考库整本书单独放、需 PQ 10-30×时） |
| **numpy.memmap flat** | ✅ 无新依赖（numpy 已在） | ✅ 必然可跑 | 磁盘驻留但**搜索仍 O(N) 暴力** | 自己做 int8 | 写文件偏移可增量 | ✅ | 中：还得自己存 ref↔offset 映射 | **保底**（只解内存不解搜索；比 sqlite-vec 还少了同库事务，不如直接 sqlite-vec） |

**结论**：sqlite-vec 既可用，备选无需启用。usearch 作为「未来真要 ANN」的逃生口已验证 wheel 在手。numpy.memmap 相比 sqlite-vec 没有任何优势（都磁盘驻留、都暴力扫描），还丢了同库同事务，不选。

---

## 调研项 3：#1 现状与改造面（file:line 实证）

**现状**（`retrieval/vector.py`）：
- `VectorRetriever.__init__`（L61-76）构造即调 `_reindex()`。
- `_reindex()`（L141-212）：`_rows_loader(store)` 全表读 → 已**只对 text_hash 变化的行重嵌**（L165-173，好底子）→ `upsert_vectors`/`prune_vectors`（已增量，L201-210）→ **但 L211 `np.vstack([_normalise(vectors[row.ref]) for row in self._rows])` 把整库堆成内存矩阵**（这是 #1 内存痛点的根）。
- `search()`（L113-139）：`scores = self._matrix @ q`（L119 暴力点积）+ `np.argsort`（L120 全排序）。
- `similarities()`（L99-111）：hybrid rerank 用，按 ref 取 `self._matrix[index] @ q`——换后端后需改为「按 ref 取回向量算 cosine」。

**SQLite 向量表**（`storage/sqlite.py`）：
- `content_vectors` / `reference_vectors` 表 schema（L109-125）：`(ref, model_id, text_hash, dim, vector BLOB, PK(ref,model_id))`。
- `get_vectors`（L673-689）/ `upsert_vectors`（L691-711, 已 ON CONFLICT upsert）/ `prune_vectors`（L713-731, 已增量删）。**这层已经是增量的**——缺的只是「索引结构本身」从内存 matrix 换成 vec0 表。

**改造面（最小侵入）**：
1. 新增 `content_vec` / `reference_vec` 两个 `vec0` 虚表（与现 `content_vectors` blob 表并存或替代——见设计取舍）。
2. `_reindex` 的 L211 `np.vstack` 删除；改为把 `vectors` 的变化行 int8 量化后 upsert 进 vec0 表（变化集已由 L163-173 的 `to_embed` 算出）。
3. `search` 的 L119-120 暴力 matmul 换成一条 `embedding MATCH ? AND k=? ORDER BY distance` SQL。
4. `similarities`（hybrid rerank 路径）改为按 ref 直接从 vec0/blob 取回向量算点积。
5. **content + reference 两表同时受益**：`VectorRetriever` 已用 `vectors_table` 参数复用（L67），换后端只改一处实现，两个 corpus（content graph + inspiration 整本书 #12）一起搞定。

**关键设计取舍（留给设计 agent）**：vec0 表本身不存原始 fp32（存 int8 时有损）。若 rerank 需要精确 cosine，保留现 `content_vectors` blob 表做「精排取原始向量」、vec0 表做「int8 粗召回」——两阶段，正好契合红线的「两阶段 rerank 兜召回」。或：vec0 直接存 fp32（无损但 4× 空间，仍磁盘驻留、仍解内存问题）。两条路都可行，实测都跑通。

---

## 调研项 4：#2 增量同步 + 共享上下文模式（file:line 实证）

### 现状：每调必全量重开 + 三套 DELETE 全表重插
- `pipeline/project.py:ProjectContext.open`（L37-74）：`load()`(L48) → `build_content_graph`(L49) → `replace_content_index`(L50) → `replace_graph_edges`(L51) → `reference_store.sync_index`(L53) → `VectorRetriever(...)`(L60, 触发 #1)。
- `mcp_server/tools.py:_project`（L234-246）：**每个工具调用** `ProjectContext.open` 后 `close`。
- `core/skills/builtin.py:default_skill_registry`（L19-33）：`bind = partial(tool, content_root=..., sqlite_path=...)`——session 参数绑进 handler，agent 每个 ReAct step 调一个 skill → 走 `tools.py` → `_project` → 全量重开。L22-24 注释自认 "tool handlers each open the project themselves (one fresh view per call)"。
- 三个 `replace_*` 全是 **DELETE 全表 + 全量重插**：`replace_content_index`（L656-671, DELETE content_index + content_fts）、`replace_graph_edges`（L816-835, DELETE graph_edges）、`replace_reference_index`（L860-924, DELETE references 三表含整本书 chunk）。

### 模式①：replace_* 改 content_hash/mtime diff 增量同步（可行方案）
现已具备的底子：vector 层已按 text_hash 增量（vector.py L165-173 + sqlite upsert/prune）。**把同样模式推广到三个 replace_***：

- **content_index/content_fts**：现 `_content_rows(bundle)`（sqlite.py L1095-1249）已为每个对象生成 `(ref, object_type, object_id, title, body)`。增量做法：
  1. 给 `content_index` 加 `row_hash` 列（`sha1(title+body)`，对齐 vector 层的 text_hash）。
  2. 进库前算当前 bundle 每行 row_hash，与库内现有行 diff：`upsert` 变化/新增行、`DELETE` 消失行（带走对应 content_fts 行——FTS5 是 external-content 还是普通表决定删法；现是普通 fts5，需手动 `DELETE FROM content_fts WHERE ref=?`）。
  3. 事务内：`BEGIN` → upsert changed + delete removed → `COMMIT`。SQLite 单连接事务天然安全。
- **graph_edges**：现 schema 无稳定主键（L128 `id AUTOINCREMENT`）。增量需给边一个确定性指纹（`source|target|kind|edge_type|valid_from|valid_until` 的 hash）或加 UNIQUE 约束，再 diff upsert/delete。比 content 略麻烦但同套路。
- **reference_index**：source 有 `text_hash`（L163），chunk 有稳定 id（`reference_chunk:{id}`）。按 source.text_hash 判断哪本书变了，只重切/重插变化的 source 的 chunk，prune 删掉的 source。整本书不变就完全跳过——参考库（#12，最大语料）受益最大。
- **mtime 快路径**：`ContentStore.load` 可先比文件 mtime/size，未变的目录直接跳过读盘+反序列化（content 文件是 source of truth）。content_hash diff 是兜底正确性，mtime 是性能快路径。

**触发点对齐**：`open` 仍要 diff 同步一次（保证库与文件一致），但从「全量 DELETE 重插」降为「只动变化对象」；`reload`（project.py L90-103）同理。这样即使共享上下文（模式②）暂不落地，单次 open 也已大幅变快。

### 模式②：session 级共享单个 ProjectContext（可行方案，且已有先例）
**项目里已存在共享上下文的先例**——`service/api.py` 的 `_registered_project`（L1461-1470）/ `_open_project_context`（L1162-1169）按 project 提供 ProjectContext（FastAPI 长驻进程）；CLI 的 `_ProjectHandle`（main.py L1316-1329）在一条命令内复用一个 context。**缺的是把这个复用推广到 agent 的 ReAct 多步 + multi_agent 多 worker。**

可行注入路径（复用既有抽象，不另起一套）：
1. **核心**：`default_skill_registry`（builtin.py L19）当前 `bind` 注入的是 `content_root`/`sqlite_path` 两个字符串，每次调用现开。改为可选注入一个**已打开的共享 `ProjectContext`**：`bind = partial(tool, project=shared_ctx)`（或注入一个返回共享 ctx 的 provider）。
2. **工具签名**：`tools.py` 的 8 个 handler 现都走 `with _project(content_root, sqlite_path) as project`。改 `_project` 为：若传入了共享 ctx 则 `yield` 它（不 open/不 close）；否则保持现「自己 open+close」行为（向后兼容，单次 CLI/测试不受影响）。这是最小侵入——handler 体内 `project.xxx` 调用一字不改。
3. **生命周期**：谁持有共享 ctx 谁负责 `close`。agent session（一次 ReAct 任务）开始时 open 一次，结束 close；multi_agent 一次任务的 DiagWorker/RepairWorker/Verifier 共享同一个 ctx。
4. **状态一致性**：现「每调重开」隐含保证了「agent 总看到最新持久化状态」（builtin.py L22-24 注释强调）。共享 ctx 后，写操作（如 `audit_project` persist issues）后续工具仍能看到——因为它们共享同一个 SQLiteStore 连接，issues 表写入即可见。需注意：若文件被外部改动（Workbench 并发写 canon），共享 ctx 不会自动 reload——需要一个显式 `reload`/增量同步触发点（模式①的 diff 同步正好用在这里：便宜，可每次 agent 任务开头跑一次）。

### 模式③：轻量工具瘦 SQLite 路径
- `list_issues`（tools.py L45-66）只查 `issues` 表（`project.sqlite_store.list_issues`），却被 `_project` 拖着付全量 load+graph+vector reindex。
- 瘦路径：给这类纯查表工具一条只 `SQLiteStore(runtime_path)`（不 load bundle、不建图、不构造 VectorRetriever）的轻量入口。`service/api.py` 已有类似分流（部分端点只读 SQLite）。注意 runtime.sqlite 是 rebuildable 运行态——若库还没被任何 full open 填充过，list_issues 瘦路径会查到空表；瘦路径仅适用于「库已建好」的 session 内，或需 fallback 到 full open。设计 agent 需定清楚这条边界。

---

## 风险与未知数

1. **「离线$0」的口径**：本机网络可用，pip 能直连 PyPI。"离线$0"是**运行时**约束（不调付费 LLM/外部服务），不是**安装期** air-gap。sqlite-vec 安装需要一次性取 wheel（292KB，已下到 `scratchpad/wheels/`，可纳入仓库 vendored wheels 或 requirements 锁定）。运行时 sqlite-vec 是纯本地 C 扩展，零网络、零$。**确认点**：设计/执行阶段把 wheel 固定（hash pin）以保证可复现离线安装。
2. **sqlite-vec 是暴力扫描非 ANN**（已实测 EXPLAIN）。30 万级 int8 KNN ≈ 0.8s/query。**这不阻塞 P0**（现状也是暴力 matmul，且 #1 真痛点是内存常驻+全量重建，sqlite-vec 都解了），但设计 agent 必须把「真 ANN」明确划给 P1 分片（#0）或未来切 usearch，别让人误以为换了 sqlite-vec 就有了次线性搜索。
3. **int8 量化召回损失**：int8 有损，KNN 排序可能与 fp32 略有出入。缓解=红线已允许的两阶段 rerank（int8 粗召回 → 原始向量精排）+ 保留 fp32 blob 表做精排。需在回归门（acceptance hit_rate）上验证不破——这是执行阶段的验收项，调研阶段无法替代。
4. **graph_edges 无稳定主键**：增量同步需先给边造确定性指纹或加 UNIQUE 约束，比 content/reference 多一步。
5. **FTS5 删除语义**：现 `content_fts` 是普通 fts5（非 external-content），增量删需手动 `DELETE FROM content_fts WHERE ref=?`，并验证 FTS5 删除性能（普通 fts5 删除是支持的，但大表删需测）。
6. **共享 ctx 的并发可见性**：Workbench 并发写 canon 文件时，长驻共享 ctx 不会自动感知。需明确 reload 触发点（模式①的增量 diff 同步可便宜地在每次 agent 任务开头跑）。
7. **usearch/faiss/LanceDB 未跑端到端**：仅验证了 download（wheel 在手）。若设计 agent 选 usearch 作主力（不推荐，sqlite-vec 够用），需补一轮端到端 import+on-disk 实测。
8. **量化在 Python 端**：0.1.9 无 `vec_quantize_i8` SQL 函数，量化逻辑（`clip(round(x*127),-127,127).astype(int8)`）落在 Python，已实测可行，但需作为确定性、可复现的一段封装（避免不同 numpy 版本 round 行为差异）。

---

## 探测脚本与证据（可复现，均在 scratchpad）
- `scratchpad/probe_sqlitevec.py`：load + f32 KNN + 增量 upsert + delete（隔离 testvenv 跑）。
- `scratchpad/probe_int8.py`：int8 KNN + 元数据列 + PARTITION KEY + `vec_quantize_i8` 探测。
- `scratchpad/probe_scale.py`：50k×1024 fp32 插入/KNN 计时/冷开库/单行 upsert。
- `scratchpad/probe_int8_scale.py`：50k×1024 int8 体积/KNN/EXPLAIN QUERY PLAN。
- `scratchpad/wheels/sqlite_vec-0.1.9-py3-none-win_amd64.whl`：含 `vec0.dll`，离线安装证据。
- `scratchpad/wheels2/usearch-2.25.3-cp313-cp313-win_amd64.whl`、`faiss_cpu-1.14.3-cp313-cp313-win_amd64.whl`：备选 wheel 可得证据。
- 隔离测试 venv：`scratchpad/testvenv/`（只装了 sqlite-vec + numpy，**项目 venv 未被污染**，实测确认 `import sqlite_vec` 在项目 venv 仍报 ModuleNotFoundError）。

---

## 给编排者的一句话总结
**sqlite-vec 在本机能用**（`enable_load_extension` 未禁用、win_amd64 wheel 自带 vec0.dll 离线可装、KNN/int8/增量/PARTITION KEY 全实跑通过）；**推荐 sqlite-vec 作为 #1 的向量后端**（与现 SQLite-everything 架构零摩擦、磁盘驻留解内存、单行 upsert 解全量重建、int8 省 4×快 3×、精确无召回损失），唯一须对设计钉死的认知是「它是暴力扫描非 ANN，真 ANN 留给 P1 分片或未来切 usearch（其 wheel 已验证在手）」。#2 的两个模式都可行且有项目内先例（`_registered_project` 共享 ctx、vector 层 text_hash 增量），增量同步把三个 `replace_*` 的 DELETE-全表-重插改成 content_hash/mtime diff 的 upsert+prune 即可。
