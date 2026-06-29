# R5（末轮）· RAG/检索栈深审 — 报告

身份：专注 RAG/检索的研究生。范围：retrieval/（vector/embedding/rerank/neural_rerank/budget/
text_match/fusion）、graph/community.py、evaluation/acceptance.py 检索 eval、+ R4 转交的下游
快照消费者（assist/contradiction.py、assist/sweep.py、inspiration/retrieval.py、
retrieval/context_pack.py）。

所有结论均有可复现脚本（见文末「复现」）。环境实测：`sentence_transformers` 已装且
**bge-m3 在本机真能加载**（dim=1024，无降级），语义路径在本环境是真的，不是搭壳。

---

## 一、R4 vector.py live-property 修复 — 强制降级端到端验证：全部 PASS（sign-off）

写了 `DegradingSemanticEmbedder`（精确复刻 SemanticEmbedder 运行时降级时序：构造期报
`st:bge-m3`，首次 `embed_many` 失败→翻到 `hashing-1024` 并 `degraded=True`），跑了 6 个端到端断言：

| 验证点 | 结论 |
|---|---|
| ① 运行时降级后 `is_semantic` 是否还撒谎 | **PASS**：降级后 `vr.is_semantic == False`，不再谎报 True |
| ② 降级 hashing 向量是否以 `st:bge-m3` 键投毒 SQLite | **PASS**：`st:bge-m3` 键下 0 行；向量全部写在 `hashing-1024` 键下 |
| ③ 重 key 逻辑在「降级中途」边界 | **PASS**：`_reindex` re-read `persist_model_id` 后丢弃旧键查找、整表 re-embed 重 key；matrix 行数 == 语料行数，search 正常 |
| ④ 后续 clean run（干净 HashingEmbedder 开同一 store）是否吃到投毒 | **PASS**：读到诚实的 `hashing-1024` 缓存，永不命中 `st:` 投毒 |
| ⑤ live property 性能/一致性副作用（每次读是否触发 embed） | **PASS**：读 `vr.model_id`/`vr.is_semantic` 100 次，`embed_many` 调用数不变——纯属性读，无副作用 |

vector.py 的 R4 改法是**对的、彻底的**。`model_id`/`is_semantic` 现在透传 `embedder.model_id`，
`_reindex` 的 lookup/persist 双段 key + 中途 re-key 正确。这一块我攻过，确认 OK。

---

## 二、【MEDIUM-LOW，真 bug，建议修，诚实低频】构造期 `model_id.startswith("st:")` 快照在 assist/contradiction.py + assist/sweep.py 仍会在运行时降级后撒谎

这是 R4 Team-C 明确转交、要本轮查的遗留项。**诚实结论：bug 真实存在且端到端可复现，但
生产触发窗口很窄。值不值得修 = 值得（与 R4 已修的 vector.py 同一类，且正中项目「不静默降级/
不自欺」红线，修法 trivial）。**

### 根因
- `assist/contradiction.py:99`：`self.embedder = embedder if _is_semantic(embedder) else None`
- `assist/sweep.py:202`：`self.embedder = embedder if (embedder is not None and _is_semantic(embedder)) else None`
- 两处都在**构造期**对 `embedder.model_id.startswith("st:")` 取**快照**。传入的是
  `project.embedder`（生产 auto 模式下是 lazy `SemanticEmbedder`，首次 embed 前 `model_id=="st:bge-m3"`）。
  若此时快照→保留 embedder→`semantic_used` 置 True；随后该消费者自己**首次** `embed_many`
  触发降级→翻成 hashing——但 `semantic_used` 仍是 True，sweep 工作单 markdown 直接写
  「已启用（bge-m3，阈值 X）」。这正是 R4 给 vector.py 修掉的「构造期快照撒谎」同型 bug。

### 端到端证据（verify_task3.py + verify_e2e_production.py）
单元层：用 lazy-degrading embedder 构造 `ThemeSweepService`/`ContradictionDetector`：
```
TASK 3a ThemeSweepService:   sweep 后 embedder=hashing-1024 degraded=True，report.semantic_used=True  ← 撒谎
TASK 3b ContradictionDetector: detect 后 embedder=hashing-1024 degraded=True，report.semantic_used=True ← 撒谎
对照（已降级再构造的正常流）: svc.embedder=None, semantic_used=False  ← 诚实
```
生产层（真走 `run_theme_sweep_action` + 真 `ProjectContext.open`，monkeypatch resolve_embedder
为 lazy degrader，磁盘建「只含 dialogue_tree」的项目）：
```
shared embedder after action: model_id=hashing-1024 degraded=True
semantic_used (reported): True
markdown semantic line: 语义近似（向量）：已启用（bge-m3，阈值 0.30），按义召回 0 个待查
>>> PRODUCTION LIE CONFIRMED：工作单宣称 bge-m3 语义召回，实际跑在 hashing stub
```

### 为什么窗口窄（诚实定级依据，不夸大）
触发要**同时**满足：
1. **bge-m3 未缓存且离线**（首跑离线才会运行时降级）——本机实测 bge-m3 能加载，正常根本不降级；
2. **content_index 为空但 bundle 有可扫对象**。`_content_rows`（sqlite.py:1095）为
   entity/quest/quest_event_ref/region/poi/dialogue/localized_text/term 各产行。只要 bundle 有
   其中**任何一类**，`VectorRetriever.__init__→_reindex` 就会在 `ProjectContext.open()` 期间先
   `embed_many` 把共享 embedder 逼降级——于是下游构造时快照已是 hashing，**诚实**。
   唯一漏网：`dialogue_trees` 和 `style_guides` 两类 sweep 扫但**不进 content_index**。所以
   触发面 = 「**零 entity/quest/region/poi/dialogue/term，仅含 dialogue_tree 或 style_guide**」的项目
   + 离线首跑。极罕见。
3. **contradiction.py 的窗口在生产中实际是关闭的**：它的语义层只扫 entity.description 和
   relation.description，二者都产 content_index 行 → 共享 embedder 必已先被 VectorRetriever 逼降级
   → 快照已诚实。所以 contradiction 的 bug 是「单元层真实、生产层够不到」。sweep 因 dialogue_tree/
   style_guide 漏洞，生产层够得到（如上 e2e 所证）。

### 建议修法（与 R4 vector.py 一致，trivial）
把构造期快照改成**调用期 live 读**：`_is_semantic` 在 `sweep()`/`detect()`/`_semantic_*` 实际用
embedder 前再判一次（或在跑完语义层后用 `getattr(embedder,'degraded',False)` 回填
`semantic_used`）。即「embed 之后再决定 semantic_used，而非构造时」。inspiration/retrieval.py 已经
是这个正确范式（读 `self.vector.is_semantic` 这个 live 属性），可直接对齐。

---

## 三、inspiration/retrieval.py + retrieval/context_pack.py — 安全，无独立快照（sign-off）

两处都读 `self.vector.is_semantic`（retrieval/vector.py:48 / context_pack.py:85），而 `is_semantic`
正是 R4 修成的 **live 属性**，且在 `build()`（调用期）读，不是构造期。实测：空 reference 语料下
embedder 保持 lazy（`st:bge-m3`），`build()` 后 `vector.is_semantic` 仍 live 反映真实后端。**它们
自己不持有任何 `model_id` 快照**，因此天然免疫 task-3 那类降级撒谎。确认 OK。

---

## 四、graph/community.py — 社区发现正确性验证：OK（sign-off）

实测两簇 + 一条跨簇 bridge 的图：
- 划分 disjoint + complete（6 成员各出现 1 次）；
- intra-community 关系 6 条全在簇内，cross-community 关系恰好那条 bridge；
- intra/cross **不相交**，且并集 = 全部 7 条关系（无丢无重）。

CNM 排序经 `sorted((-len, g[0]))` 稳定化（文档诚实标注「不保证跨 networkx 版本 bit-for-bit」，
不冒充 DETERMINISTIC，并给 leiden opt-in）。relay-bridging 投影逻辑正确（事件经 qer 中继桥接进 quest
社区），且作者已在 docstring 标注 O(Σkᵢ²) 在 shared-key hub 下的 scaling 风险并给缓解方向——诚实。
未发现 bug。

---

## 五、evaluation/acceptance.py 检索 eval — 真测语义、定标诚实：OK（sign-off）

- acceptance gate **故意 pin HashingEmbedder**（line 864/992），docstring 反复声明「只证 BM25 可复现性，
  **不**证 bge-m3 语义召回」——不自欺。
- 真语义对比在 `run_semantic_retrieval_benchmark()`：用**真 SemanticEmbedder**（line 703），
  CI 跳过保 $0；查询集 `retrieval_eval_queries()` 全部刻意**不含实体名**（按功能/历史/关系/跨语种
  描述），BM25 应低分、bge-m3 应胜出——确实在测语义而非关键词。
- 降级一致性无洞：benchmark 先 `semantic_available()`（会经 lru_cache `_load_model` 真加载并缓存），
  通过后 `SemanticEmbedder()` 首次 embed 命中缓存模型，**不会再降级**——即「能进对比的前提就是模型已
  加载」，delta 不会被「双 hashing」静默污染。
- 样本量诚实标注（n=15/30 太小，需 n≥100 才有 Wilson CI<±5%），明说是作品集 demo 非生产基准。
- `qa_citation_existence_or_refuse` 已按 R3/R4 诚实命名，按简报属已定非 bug，不重复报。

未发现 bug。

---

## 六、neural_rerank.py / rerank / budget / text_match — 抽查：真接模型，无搭壳（sign-off）

- `NeuralReranker` 是**真 cross-encoder**：`FlagEmbedding.FlagReranker` 优先、`sentence_transformers.
  CrossEncoder` 兜底，`predict(pairs)` 联合 cross-attention 打分，不是 bi-encoder/词面冒充。
- 降级 fail-loud：模型加载或 predict 失败→ WARNING + 退词面，并把 `source` 打成
  `reranked_lexical_fallback`（机读信号，与正常 `reranked_neural` 区分）——不静默降级。
  注：reranker 没有像 embedder 那样的 `degraded` 布尔，但 `source` 字段已是机读降级标记，不构成自欺。
- env 门（auto/neural/lexical/none）语义清晰，CI 默认 lexical 保 $0 确定性。

---

## 末轮 sign-off 汇总

- **攻过且确认 OK**：retrieval/vector.py（R4 live-property + 重 key，6 项强制降级 e2e 全绿）、
  inspiration/retrieval.py、retrieval/context_pack.py、graph/community.py（划分/intra-cross 正确性）、
  evaluation/acceptance.py（真测语义 + 定标诚实 + 无降级污染）、neural_rerank.py（真 cross-encoder +
  fail-loud）。
- **新发现真 bug 1 个（MEDIUM-LOW）**：assist/sweep.py（生产可达）+ assist/contradiction.py（单元真实/
  生产窗口关闭）构造期 `model_id.startswith("st:")` 快照，运行时降级后 `semantic_used` 撒谎。与 R4 已修
  vector.py 同型，修法 trivial（改 live 读 / embed 后回填）。诚实定级：低频但触正中「不自欺」红线，值得修。
- **未犯前轮误报**：确认 bge-m3 dense 无 instruction prefix 是官方规范（embedding.py 已权威标注），
  本机实测 bge-m3 真加载、dim=1024，语义路径非搭壳——不报「缺前缀」「假语义」之类。

## 复现
脚本在 scratchpad/：
- `verify_degrade.py`：vector.py 6 项强制降级（TEST1-5 全 PASS，TEST6 确认空语料留窗口）
- `verify_task3.py`：sweep/contradiction 单元层撒谎 + 正常流诚实对照
- `verify_e2e_production.py`：真 `run_theme_sweep_action` 生产路径撒谎实证（dialogue_tree-only 项目）
运行：`.venv/Scripts/python.exe scratchpad/<name>.py`
