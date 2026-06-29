# OWCopilot 规模化只读审计 (SCALE_AUDIT)

只读审计，未改任何代码。目标规模假设：**5 年 / 近百版本 / 几万~几十万条 chunk·对象 / 跑在开发者内存受限的笔记本上**。

核查的核心反假设：现实现处处假设「单世界 + 几百对象 + 整体进内存 + 全量扫描/精确计算就够」。下面逐条核实。每条给：① 位置 ② 现做法（小规模假设）③ 大规模会怎样崩/退化 ④ 受限本地硬件下修正方向 ⑤ 严重度 + 改动量级。

严重度图例：**🔴 真会崩（必修）** / **🟠 会退化但能忍** / **🟢 不受影响（已 sign-off）**。

---

## 0. 最深的结构性结论（先看这个）

整个系统是 **单世界、无版本/scope 维度** 的：

- `content/models.py` 没有 `world_id` / `scope` / `tenant` 字段；`Entity.version` 只是一条自由文本（L65），不是分区维度。
- `content/store.py` 一个 `content_root` = 一个扁平世界，`load()` 把 `world/entities/*.json`、`quests/*.json` 等**整个目录全量读进一个 `ContentBundle`**（内存大 dict）。
- 因此 “5 年百版本” 的几十万对象，要么全塞进一个 `content_root`（一次 `load()` 全进内存），要么靠人手开多个 `content_root`（产品里没有这个概念）。**这是所有下游瓶颈的根**：RAG 矩阵、图、审计、impact、context_hash 全都建立在 “bundle 已整体在内存” 之上。

下面的每一条几乎都是这个根的派生。

---

## 修正清单（按严重度排序）

### 🔴 #1 稠密向量检索：全语料进内存矩阵 + 暴力 matmul + 任意改动全量重建
- **位置**：`retrieval/vector.py:54-212`（`VectorRetriever`），核心：
  - `_reindex()` L141-212：每次构造都 `load_content_rows()`（`storage/sqlite.py:34-41` 全表 `SELECT … content_index`）→ `np.vstack([... for row in self._rows])`（L211）把**整个语料堆成一个 numpy 矩阵**常驻内存。
  - `search()` L113-139：`scores = self._matrix @ q`（L119）对全矩阵做暴力点积 + `np.argsort` 全排序（L120）。
  - 退化路径 L185-199：embedder 中途降级时**整库重嵌 + 重建**。
- **现做法（小规模假设）**：文件头注释明说 “exact … fast at lore scale”。bge-m3 维度 1024 fp32。
- **大规模会怎样**：
  - **内存**：30 万 chunk × 1024 dim × 4B ≈ **1.2 GB** 单一 `float32` 矩阵常驻（外加 SQLite blob 读出的临时副本、`vstack` 拷贝峰值 ~2 倍）。笔记本上每开一个项目就吃掉 1–2 GB，**OOM 风险真实**。
  - **每次 search**：30 万行 matmul + 全排序，单线程 numpy 约几十~上百 ms/query，agent 一轮多次检索就线性叠加。
  - **构造期**：`ProjectContext.open()` 一调用就触发 `_reindex()`（见 #2），即使一行没变也要把 1.2 GB 从 SQLite 反序列化 + `np.frombuffer` + `vstack`。
- **修正方向（本地内存受限优先）**：
  - 换**磁盘驻留 ANN**，不要 in-memory HNSW（HNSW 图常驻内存，违反受限硬件约束）。推荐顺序见末尾《RAG 该换成什么》——首选 **sqlite-vec**（已经在用 SQLite，零新进程，磁盘驻留，支持 PQ/量化）或 **LanceDB**（列式磁盘 + IVF-PQ，mmap 不全进内存）。
  - 向量 **int8/PQ 量化**：1024 dim fp32→int8 直接省 4×，PQ 可省 10–30×。
  - **增量索引**：现在 text_hash 增量嵌入已经做了（L165-173 只嵌变化行，赞），但**矩阵仍全量 vstack**——增量嵌入省的是 LLM 成本，没省内存/重建成本。换 ANN 后增量 upsert 即可。
- **严重度 + 量级**：🔴 必修。**大改**（换检索后端 + schema + 量化），1–2 周量级。这是 RAG 侧头号瓶颈。

---

### 🔴 #2 每次工具调用 / 每个 agent step 都全量重开项目（ProjectContext.open）
- **位置**：`pipeline/project.py:37-74`（`open`）+ `mcp_server/tools.py:234-246`（`_project` 上下文管理器，**每个 MCP/skill 工具调用都 open+close 一次**）。
- **现做法**：`open()` 做的全是 O(全语料) 的重活：
  1. `content_store.load()`（全目录读进 bundle，L48）；
  2. `build_content_graph(bundle)`（全图重建，L49）；
  3. `replace_content_index(bundle)` —— `storage/sqlite.py:656` **`DELETE FROM content_index` + `DELETE content_fts` 然后全量 re-INSERT**（L657-671）；
  4. `replace_graph_edges` —— 同样 **DELETE 全表 + 全量重插**（`sqlite.py:816-835`）；
  5. `reference_store.sync_index` —— `replace_reference_index` **DELETE references 三表 + 全量重插**（`sqlite.py:860-924`，含整本书的 chunk）；
  6. `VectorRetriever(...)` 构造 → 触发 #1 的 `_reindex()`（全矩阵）。
- **大规模会怎样**：`core/skills/builtin.py:24` 的注释明说 “tool handlers each open the project themselves (one fresh view per call)”——也就是 **agent 每个 ReAct step 调一个工具（`audit_project`/`list_issues`/`build_context_pack`/`impact_of`…）就把几十万对象重新 load + 重建图 + 三套 SQLite 全表 DELETE/重插 + 重堆 1.2 GB 向量矩阵**。多 agent（`multi_agent/`）里 DiagWorker→RepairWorker→Verifier 串行，每个 worker 的每步又各开一次。这不是退化，是**几十万对象下单步秒级~十秒级、整跑分钟级**，且每次写穿整库 I/O。
  - `list_issues` 这种本来纯查 SQLite 的操作，也被迫付全量 load + reindex 的开销（它只需要 `issues` 表）。
- **修正方向**：
  - **持久化运行态**：runtime SQLite 落盘后，`content_index`/`content_fts`/`graph_edges`/向量表应做**增量同步**（按文件 mtime / content_hash diff，只更新变化对象），而不是每开必 DELETE 全表重插。`replace_*` 改成 `upsert_changed + prune_removed`。
  - **复用打开的项目**：长驻进程（Workbench、agent session、multi_agent 一次任务）应**共享一个 ProjectContext**，工具不再各自 open；`pipeline/project.py:76-88` 的 `qa_context_builder()` 已经意识到 “building a fresh one per question re-reads/re-stacks every vector for nothing”——把这个认知推广到所有工具。
  - 轻量工具（`list_issues`）走 **只连 SQLite 的瘦路径**，不触发 load/graph/vector。
- **严重度 + 量级**：🔴 必修。**大改**（生命周期重构 + 增量同步），与 #1 同级。**这是 Agent 侧头号瓶颈**——比单个工具本身的复杂度更致命。

---

### 🔴 #3 BM25 的 fallback 路径全表线性扫描 + Python 逐行打分
- **位置**：`retrieval/bm25.py:49-68`（`_fallback_search`）+ `storage/sqlite.py:972-997`（reference 同款 fallback）。
- **现做法**：当 `build_fts_match_query` 返回 None（query 只剩停用词/标点/纯 CJK 被分词成空），或 FTS 命中不足 `limit` 时（L42-46），就 `SELECT … FROM content_index ORDER BY ref`（**全表**）然后 Python 里逐行 `lexical_score`（L54）。
- **大规模会怎样**：几十万行全拉进 Python + 逐行子串打分 = 每次 fallback **O(N) 行 × O(query·body) 字符**。中文 query 很容易触发（FTS5 默认 unicode61 分词器对 CJK 不友好，`build_fts_match_query` 用 `\w+` 抓 token，长串 CJK 可能整段成一个 token 或匹配很差），**fallback 不是罕见路径**。一次几十万行的 Python 扫描就是几百 ms~秒级，且把全部 body 文本拉进内存。
- **修正方向**：
  - 给 FTS5 配 **CJK 友好分词器**（ICU tokenizer 或 trigram，SQLite 3.34+ 有 `trigram`），让中文 query 真正走 FTS 索引，**消除 fallback 的触发频率**。
  - fallback 本身**加 LIMIT 并下推到 SQL**（不能把全表拉进 Python 排序）；或干脆在大库下禁用全表 fallback，宁可少召回也不 O(N) 扫描。
- **严重度 + 量级**：🔴 必修（大库下高频触发）。**中改**（换分词器 + fallback 限流），数天量级。

---

### 🟠 #4 一致性审计：全部规则全量遍历 bundle，每跑一次重头算
- **位置**：`audit/runner.py:40-62`（`run`）；规则如 `audit/rules/reference_rules.py:102-126`（`_entity_references` 遍历所有 quests/pois/dialogues/event_refs）、`graph_rules.py`（遍历所有 relations、跑 `nx.find_cycle` 全图）、`audit/context.py:19-25`（`from_bundle` 每次 `build_content_graph` 全图 + `content_hash` 全 bundle 序列化）。
- **现做法**：`audit_project`（`mcp_server/tools.py:27-42`）→ `run_full_audit` → 每条规则 `check(ctx)` 对全量对象做一遍。无增量、无脏标记、无 “只审改动的子图”。`content_hash(project.bundle)`（tools.py:38）**把整个 bundle 序列化成 JSON 算 sha256**，几十万对象每次审计都全序列化一遍。
- **大规模会怎样**：
  - 单次全量审计 = O(对象数 + 关系数 + 图规模)，几十万对象下**单次秒级~数十秒**；`content_hash` 全序列化本身就是几百 MB JSON。
  - **真正致命的是叠加 #2**：verifier 每次验证都独立 `audit_project`（`multi_agent/verifier.py:206-244` `_deterministic_verify`），worker 也审，agent ReAct 每轮可能审——**每次审计都重头跑全量规则 + 全图 + 全序列化 hash**。
- **修正方向**：
  - **增量审计**：按版本 diff（`content/snapshot.py` 已有 `bundle_diff`）只重审受影响对象及其图邻域；引用类规则用倒排索引（“谁引用了 X”）而非每次重扫。
  - `content_hash` 改成**增量/分块 Merkle**（对象级 hash 缓存，只重算变化对象），不要每次全序列化。
  - 审计结果按 content_hash 缓存（`audit_runs` 表已有 content_hash 列，L42-48 schema，但没用它做 “未变则跳过”）。
- **严重度 + 量级**：🟠 退化（功能不崩，但慢且和 #2 复合放大）。**中~大改**（增量审计框架）。单世界小库能忍，目标规模必须做。

---

### 🟠 #5 GraphRAG 社区发现：全图重建 + CNM 全图跑 + relay 桥接 O(Σk²)
- **位置**：`graph/community.py:83-118`（`detect_communities`，每次 `build_content_graph(bundle)` **全图重建** L96，再 `nx.community.greedy_modularity_communities` 全图跑 L105）；`_projection` L143-204 的 relay 桥接 L199-203 是 **O(Σ kᵢ²)**；`cross_community_relations` L121-140 又 `build_content_graph` 再建一次全图。
- **现做法**：文件头注释诚实写了 “good default for worlds in the **~50–300 node** range”，并在 L160-170 明确标注了 relay 桥接的二次复杂度悬崖（“≈125k edges for N=500” 共享 localization key 的 hub）。
- **大规模会怎样**：
  - CNM/Leiden 在几万~几十万节点上是**分钟级甚至更糟**，且 networkx 全图常驻内存。
  - relay 桥接的 O(Σk²)：一个被 N 个 quest 共享的 `localization:<key>` 就产生 C(N,2) 条边——大世界里共享 key 是常态，**真能炸成几百万条边**。
  - 缓解点：社区索引是**显式触发**的（`cli/main.py:862`、`app/actions.py:201`），不是每问一次（`qa_context_builder` 只是把已存的 report 当 hit 检索，`retrieval/community_reports.py:28-42` 读 `list_community_reports` 全表但那是 report 级，数量远小于对象数）。所以**不是每查询付费**，是**每次重建索引付费**。
- **修正方向**：
  - 社区检测**增量化**：按版本 diff 只对变化的子图重分区；或分层（先按 region/version 粗分再社区检测）。
  - relay 桥接**加度数上限**（注释里自己提的：“cap a relay's neighbour count … or switch to a sparser scheme”）——这条建议落地即可。
  - 大库换 Leiden（已 opt-in）并限制在子图上跑。
- **严重度 + 量级**：🟠 退化（且 relay 桥接有真实二次炸点）。**中改**。注释已诚实标注，属 “已知未做”。

---

### 🟠 #6 关系/relation 检索补全：全表扫 relation 行 + Python 过滤
- **位置**：`storage/sqlite.py:733-750`（`relation_rows_for_entities`）；`retrieval/context_pack.py:69-79` 每次 build 都调它。
- **现做法**：`SELECT … WHERE object_type='relation'`（**拉所有 relation 行**）再 Python 里 `tokens[0]/tokens[-1] in entity_ids` 逐行过滤（L744-749）。
- **大规模会怎样**：几十万 relation（百版本叙事里关系数通常 > 实体数）每次 context pack 都全拉 + 全 Python 过滤。`build_context_pack` 是 agent 高频工具，**每次检索都 O(关系总数)**。
- **修正方向**：relation 的 source/target 建**真索引**（单独 `relation_endpoints(entity_id, relation_ref)` 表或在 `content_index` 上加 source/target 列 + 索引），用 `WHERE source IN (?) OR target IN (?)` 走索引，而不是全表 + Python。
- **严重度 + 量级**：🟠 退化（高频路径，O(N) 关系）。**小~中改**（加索引表）。

---

### 🟠 #7 Rerank / fusion / budget：候选规模可控，但全量 token 估算有成本
- **位置**：`retrieval/fusion.py:10-29`（RRF）、`retrieval/rerank.py:103-170`（LexicalReScorer）、`retrieval/neural_rerank.py:148-201`（NeuralReranker `predict` 全候选对）、`retrieval/budget.py:56-100`（`estimate_tokens` 每 hit 调 tokenizer）。
- **现做法**：候选集来自各 retriever 的 `limit`（默认 10），fusion/rerank 只在**这个小候选集**上跑——**规模无关，✅ 设计正确**。但：
  - `context_pack.py:55-64`：`build_expanded` 对 `queries`（原 query + 多个 variant）× 每个 retriever（bm25/vector/graph）各跑一次，候选 = O(变体数 × retriever 数 × limit)，仍小。
  - `budget.py:88-99`：对每个保留 hit 调 `estimate_tokens`（bge-m3 AutoTokenizer），候选小所以可控。
  - graph_expand 的 `_relation_text`（`retrieval/graph_expand.py:134-146`）：**每个 hit** 都 `for edge in self.graph.edge_refs(edge_type="relation")` **遍历全图所有 relation 边** + 内层再 pair_kinds 全配对——这是隐藏的 **O(hits × 全relation边)**，大库下退化（见 #6 同源问题）。
- **大规模会怎样**：rerank/fusion 本身不退化（候选小）。但 `_relation_text` 每 hit 全图扫边，graph leg 召回 10 个 hit 就 10×全relation；NeuralReranker 模型常驻内存（~280MB），本地受限要注意但候选只 10 对，可接受。
- **修正方向**：`_relation_text` 用 #6 的 relation 索引按 ref 直查邻接边，别每 hit 全图扫。其余维持。
- **严重度 + 量级**：🟠 仅 `_relation_text` 退化（其余 🟢）。**小改**（复用 #6 的索引）。

---

### 🟠 #8 影响分析 impact_of：单点 BFS 受 radius 限，但建图成本在 #2
- **位置**：`impact/analyzer.py:13-40`（`analyze`）；`graph/index.py:77-94`（`ego_distances` 用 `nx.single_source_shortest_path_length(cutoff=radius)`）。
- **现做法**：每个 change 做一次 **半径受限 BFS**（radius 默认 2），BFS 本身只触达局部邻域，**算法层面规模友好 ✅**。`ContentGraph` 还 memoize 了 filtered/undirected 视图（`index.py:240-257`）避免每次 deep-copy。
- **大规模会怎样**：BFS 局部，OK。但 **`_undirected_filtered_graph` 第一次调用要 `.to_undirected()` 整张图**（`index.py:253-257`）——几十万节点的 MultiDiGraph 转无向 = 一次性全图拷贝，常驻内存。加上 #2 每次 open 都重建整图，impact_of 的真实成本在建图/转无向，不在 BFS。
- **修正方向**：图持久化/复用（同 #2）；`to_undirected` 大库下改成在持久化图存储（如图数据库 / 邻接表落盘）上做局部 BFS，不全图进内存转换。
- **严重度 + 量级**：🟠 算法 OK，成本在图生命周期（归 #2）。**中改**（随 #2 一起）。

---

### 🟢 #9 Snapshot 版本历史：全 bundle dump，但本就是冷路径
- **位置**：`content/snapshot.py:70-87`（`write_snapshot` 全 bundle dump 成一个 JSON）、`bundle_diff` L124-166（两个全 bundle 的 `model_dump` 全量 diff）。
- **现做法**：每个快照 = 整世界一个 JSON 文件；diff = 两个全 bundle 反序列化 + 逐 kind 全 key 比对。
- **大规模会怎样**：几十万对象一个快照文件几百 MB；百版本 = 几十 GB 磁盘 + 每次 diff 全量反序列化。**但这是显式的人工操作（打快照/看历史），不是热路径**，也不进 agent 循环。磁盘可忍，diff 慢可接受。
- **修正方向**（非紧急）：快照改对象级/增量（只存 diff）；`bundle_diff` 用 content_hash 先粗筛未变对象。
- **严重度 + 量级**：🟢 冷路径，能忍。若真上百版本再做增量快照。**中改**，可延后。

---

### 🟢 #10 ReAct transcript 压缩 / observation 截断：与语料规模无关
- **位置**：`agent/react.py:42`（`_OBSERVATION_CHAR_LIMIT=4000` 截断单个工具输出）、`agent/context_compressor.py`（增量压缩 + `CompressionCache`）。
- **现做法**：observation 截断 + transcript token 预算 + 增量压缩缓存，都做得很到位（append-only、O(steps)→O(1) 增量压缩）。
- **大规模会怎样**：transcript 规模取决于 **step 数**，不取决于语料规模。`audit_project` 在几十万对象下返回的 `issues` 列表可能巨大（`mcp_server/tools.py:39` dump 全部 issues），4000 字符截断会**截掉大量 issue**——但那是 #2/#4 的产物，压缩层本身没问题。
- **修正方向**：工具输出本身分页/限量（`audit_project` 返回 top-N + 计数，别 dump 全量 issues）。
- **严重度 + 量级**：🟢 压缩层本身不受影响。工具输出限量属 #4 范畴。

---

### 🟢 #11 评测 acceptance gate：门槛只在小语料下成立（设计如此）
- **位置**：`evaluation/acceptance.py`：`RETRIEVAL_HIT_RATE_GATE=0.90`/`RETRIEVAL_TIGHT_HIT_RATE_GATE=0.95`（L57-62）、`_retrieval_hit_rate`（L825-837）在 **~65 实体的固定世界** 上跑 30 条 verbatim query。
- **现做法**：固定构造 65 实体世界 + 25 seeded error + 30 query，断言 hit_rate≥0.9。文件头与 query 注释**已极其诚实**地标注：“n=15/30 太小不足以统计显著”、“verbatim query 主要测 BM25 可靠性不是语义召回”、“HashingEmbedder pin 只证明 BM25 可复现”。
- **大规模会怎样**：这些门**在几十万对象上不成立也不应该成立**——hit_rate=1.0 依赖的是 “全量精确检索 + 小世界里 top-k 必含答案”。一旦换 ANN（#1）有近似召回、语料百倍，verbatim top-k 命中率必然下降，**这些门对真实规模没有意义**（也从没声称有：它是 CI 回归门 + 作品集 demo，不是生产基准）。
- **修正方向**：这是**评测口径问题不是 bug**。规模化后需要：(a) 大语料合成评测集（n≥100，带 Wilson CI）；(b) 召回门改 recall@k 而非精确 hit；(c) 区分 “功能正确性回归门（小世界，保留）” vs “规模性能/召回基准（新建）”。
- **严重度 + 量级**：🟢 已 sign-off（诚实标注）。**新增工作**（大规模评测集），非修复。

---

### 🟢 #12 inspiration 参考库检索：复用 #1 的矩阵 —— 同病，且更可能是最大语料
- **位置**：`inspiration/retrieval.py:23-65`（`ReferenceContextBuilder` 复用 `VectorRetriever` + `reference_vectors` 表）；`inspiration/store.py:93-118`（`load_chunks` 把**所有 source 的所有 chunk** 全读）、`sync_index` 全量 `replace_reference_index`。
- **现做法**：和 #1 完全同一个 `VectorRetriever`（brute-force 矩阵），但**参考库装的是 “整本小说”**（store.py 注释 L60-61、105：“a 2M-char book is ~1100 chunks”）。一个项目导入十几本书就是几万 chunk，加上百版本的项目语料，参考库本身可能是**最大的那块语料**。
- **大规模会怎样**：与 #1 相同（内存矩阵 OOM + 暴力 matmul + 全量 reindex），且 `ReferenceContextBuilder` 在 `ProjectContext.open` 里也被构造（project.py:72）→ 每次 open 重 reindex 参考库矩阵。
- **修正方向**：随 #1 一起换磁盘 ANN（同一个 `VectorRetriever` 改造即覆盖 content + reference 两个表）。
- **严重度 + 量级**：🟢 标 🟢 仅因 “是 #1 的同源问题不重复计”；实际严重度 = #1（🔴）。**随 #1 一并修**。

---

## 专题一：RAG 该换成什么（本地内存受限权衡）

**约束**：开发者笔记本，内存受限，离线 $0，已重度依赖 SQLite（runtime + FTS5 都在 SQLite 里），要求**磁盘驻留 / 量化 / 增量**，明确**不要 in-memory HNSW**。

**推荐排序**：

1. **首选：`sqlite-vec`（磁盘驻留向量 + 现有 SQLite 内聚）**
   - 理由：现仓库已经是 “SQLite 单文件 + FTS5 BM25 + 向量 blob 表（`content_vectors`/`reference_vectors`）” 的形态（`storage/sqlite.py:109-125`）。sqlite-vec 让向量索引**就活在同一个 .sqlite 文件**里，零新进程、零新部署、天然磁盘驻留，BM25(FTS5) 与向量同库同事务，**hybrid 检索不再需要 Python 端 vstack 矩阵**。
   - 支持 int8 量化（省 4×）；query 走索引而非全表 matmul；增量 upsert 单行即可（彻底解决 #1 的 “任意改动全量重建”）。
   - 迁移成本最低：`VectorRetriever.search` 从 `matrix @ q` 改成一条 `vec` KNN SQL；`_reindex` 从 vstack 改成 upsert 变化行。content + reference 两个表同时受益。

2. **次选：LanceDB（列式磁盘 + IVF-PQ，mmap 不全进内存）**
   - 理由：原生 IVF-PQ，PQ 把 1024-dim 压到几十字节（省 10–30×），列式 + mmap 按需载入，明确为 “比内存大的向量集” 设计，离线本地可跑。
   - 代价：引入一个新存储（与 SQLite 并存），运行态一致性/事务要自己协调；比 sqlite-vec 重。适合参考库（#12，整本书）这种最大语料单独放。

3. **可选量化层（不论后端都该上）**：fp32→**int8** 是免费 4× 内存；**IVF-PQ** 适合 ≥10 万向量。受限硬件下 PQ 的 “有损召回” 用两阶段（PQ 粗召回 → 原始向量精排小候选）补回精度。

4. **明确不选**：
   - **in-memory HNSW（faiss HNSW / hnswlib）**：图常驻内存，几十万 × 1024 的 HNSW 图 + 向量 ≥ 数 GB，**直接违反 “内存受限笔记本” 约束**。
   - **外部向量服务（Qdrant/Weaviate/Milvus server）**：违反 “本地优先 / 离线 $0 / 单机笔记本”。
   - **DiskANN**：磁盘驻留理念对，但工程重、构建慢、Windows 本地落地成本高，作品集场景性价比低于 sqlite-vec。

**增量索引要点**：
- 现已具备的好底子：`content_vectors` 按 `(ref, model_id, text_hash)` 缓存，`_reindex` 已只嵌变化行（vector.py:165-173）、`prune_vectors` 已清陈旧（sqlite.py:713-731）。**缺的只是 “索引结构本身的增量”**——换成 sqlite-vec/LanceDB 后，把 `np.vstack` 全量重建替换为 “变化行 upsert + 删除行 delete”，矩阵重建成本归零。
- 把 `ProjectContext.open` 的 `replace_content_index`/`replace_graph_edges`/`replace_reference_index`（全 DELETE+重插）改成**按 content_hash/mtime diff 的增量同步**（见 #2），否则即使向量增量了，索引表仍每次全量重写。

**按版本 / scope 分片要点**：
- **加分区维度**：在 `content/models.py` 与 SQLite 表加 `world_id` / `version`（或 `branch`）列，检索默认 scope 到 “当前世界 + 当前版本（含其继承的基线）”，而不是全 30 万一锅检索。这同时让 RAG 候选集天然变小、让审计/impact 可只跑 active scope。
- **冷热分层**：历史版本（百版本里绝大多数是历史）走 “归档分片 / 按需挂载”，只有当前活跃版本的索引常驻；历史检索按需 lazy-load 对应分片。
- 分片是 #0 “单世界无 scope” 根问题的正解，**优先级最高的架构性改造**——它让 #1/#2/#4/#5 的 N 从 “全历史几十万” 降到 “当前版本几千~几万”，很多 🟠 不修也就能忍了。

---

## 专题二：Agent 侧 —— 哪些工具/流程要从 “全量扫描” 改成 “索引/增量/局部”

| 工具 / 流程 | 现在（全量） | 改成 |
|---|---|---|
| **所有工具的项目打开** `mcp_server/tools.py:_project` + `pipeline/project.py:open` | 每个工具调用全量 load+建图+三套全表 DELETE/重插+重堆向量矩阵（#2） | session 级**复用单个 ProjectContext**；轻量工具走瘦 SQLite 路径；`replace_*` 改增量同步 |
| `audit_project` `tools.py:27` | 每次全量规则 + 全图 + 全 bundle `content_hash`（#4）；返回**全部** issues 后被 4000 字符截断（#10） | 增量审计（按 snapshot diff 审受影响子图）；content_hash 缓存跳过未变；返回 **top-N + 计数**，分页 |
| `build_context_pack` `tools.py:69` | 触发 #1 暴力 matmul + #6 全表 relation 扫 + graph leg `_relation_text` 全图扫边（#7） | ANN 检索（#1）；relation 走索引（#6）；`_relation_text` 按 ref 查邻接 |
| `list_issues` `tools.py:45` | 被 `_project` 拖着付全量 load+reindex，实际只查 `issues` 表 | 纯 SQLite 瘦路径，不开 bundle/graph/vector |
| `impact_of` `tools.py:121` | BFS 局部✅，但每次重建整图 + `to_undirected` 全图拷贝（#8） | 复用持久化/共享图；大库下局部 BFS over 落盘邻接表 |
| **multi_agent** `verifier.py:_deterministic_verify` / `workers.py` | Verifier 每次独立重跑 `audit_project`（全量审计）；DiagWorker/RepairWorker 各自每步重开项目 | 共享一次审计结果（content_hash 未变即复用）；worker 共享 ProjectContext；verifier 读缓存审计而非重跑 |
| **community index** `qa/community_index.py:build` | 每次 build 全图重建 + 全图社区检测 + relay O(Σk²)（#5） | 增量社区检测（仅变化子图）；relay 度数上限；按 scope 分区先粗分 |

**Agent 侧总结**：单个工具的算法多数是对的（BFS 局部、rerank 候选小、压缩增量）。**真正的规模杀手是 “生命周期” 而非 “算法”**——`ProjectContext.open` 把 O(全语料) 的重活塞进了 “每个工具每次调用”，再被 ReAct 多步 × multi-agent 多 worker 放大。修 #2（共享 + 增量同步）能一次性拔掉 Agent 侧最大的钉子。

---

## 总结：离 “支撑 5 年百版本长线叙事” 还差哪几块 + 优先级

**差的几块（按架构层）**：
1. **无 scope/版本分片**（#0）——一切的根，全系统假设单一扁平内存世界。
2. **RAG 全进内存矩阵**（#1/#12）——内存 OOM + 每查暴力 matmul + 全量重建。
3. **运行态全量重建 + 工具每调必重开**（#2）——Agent 侧最大放大器。
4. **审计/社区/关系检索全量扫描**（#4/#5/#6）——无增量、无索引、有二次炸点（relay）。
5. **评测口径只覆盖小世界**（#11）——不是 bug，但规模化需要新基准。

**改造优先级与工作量级**：

| 优先级 | 项 | 量级 | 理由 |
|---|---|---|---|
| **P0** | #2 共享 ProjectContext + 增量同步 `replace_*` | 大（1–2 周） | Agent 侧最大瓶颈，几乎所有热路径的成本放大器；不依赖换检索后端就能先做 |
| **P0** | #1+#12 换磁盘驻留向量后端（sqlite-vec 首选）+ 量化 | 大（1–2 周） | RAG 侧最大瓶颈 + 内存 OOM 风险；与 #2 解耦可并行 |
| **P1** | #0 加 `world_id`/`version` 分片维度 | 大（架构性） | 治本：把所有 N 从 “全历史几十万” 降到 “当前版本几千”，连带缓解 #4/#5 |
| **P1** | #3 FTS5 CJK 分词器 + fallback 限流 | 中（数天） | 中文为主的产品，全表 fallback 高频触发 |
| **P2** | #4 增量审计 + content_hash 缓存 + 工具输出分页 | 中~大 | 与 #2 复合放大；分片后可缓解 |
| **P2** | #6/#7 relation 索引（消除全表 relation 扫 + `_relation_text` 全图扫） | 小~中 | 高频 context_pack 路径 |
| **P3** | #5 增量社区检测 + relay 度数上限 | 中 | 索引非热路径；relay 上限是注释自己提的现成建议 |
| **P3** | #11 大规模评测集（n≥100, recall@k, Wilson CI） | 新增 | 规模化后才有意义 |
| **P4** | #9 增量快照 | 中 | 冷路径，可最后做 |

**一句话**：算法层基本健康，**真正缺的是 “分片 + 磁盘驻留 + 增量” 这三件套**；最致命的两个是 RAG 的全内存矩阵（#1）和 Agent 每步全量重开项目（#2），两者都是 🔴 必修、大改量级。
