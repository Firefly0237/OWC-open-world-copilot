# R4 · RAG/检索栈深审（专注检索的研究生视角）

HEAD = R3 fixes。验证范围：retrieval/{embedding,vector,rerank,neural_rerank,budget,text_match,
context_pack}、graph/community.py、evaluation/acceptance.py。所有结论附可复现脚本
（`scratchpad/rag_probe*.py`，UTF-8 运行）。

---

## 【高 / 真 bug】runtime embedder 降级对 VectorRetriever 不可见 → 静默降级 + 缓存投毒

**这是 R3 新增 `degraded` 标记本应防住、但没接通的那个洞。直接踩红线 #1（不静默降级/不自欺）。**

### 根因
- `SemanticEmbedder` 懒加载：`model_id="st:bge-m3"`、`degraded=False`，模型在**首次 embed**才加载。
  离线首跑加载失败时，`embed_many` 捕获异常 → `degraded=True`、`model_id` 翻成
  `hashing-1024`（embedding.py:104-117）。**这一层是对的。**
- 但 `VectorRetriever.__init__` 在 **line 71** 一次性快照 `self.model_id = self.embedder.model_id`，
  **早于** line 77 的 `_reindex()`。`_reindex` 内部才触发首次 `embed_many` → 触发降级。
  此时 embedder 的 `model_id` 已翻成 hashing，但 `VectorRetriever.model_id` 仍是 `st:bge-m3`。
- `is_semantic`（vector.py:80-82）= `model_id.startswith("st:")` → 永远返回 **True**。
- **没有任何生产代码读 `embedder.degraded`**（全仓 grep：只有 embedding.py 自己 set + 一个专测文件）。
  R3 加的机读标记是**孤儿**——set 了、测了，但下游零消费。

### 两个叠加后果（都已复现）
1. **静默降级**：`context_pack.py:85` `if self.vector.is_semantic:` → True，于是把
   HashingEmbedder 的 char-overlap 余弦当作 bge-m3 语义分喂进 `rerank_hits(semantic_scores=...)`。
   rerank 把字符重叠噪声当"语义"，且**无任何机读信号**告诉上层"这其实是 BM25-only"。
2. **缓存投毒（跨进程持久）**：降级产出的 hashing 向量被 `_reindex` 以 `st:BAAI/bge-m3` 这个
   **model_id key 持久化进 SQLite**（vector.py:154）。下次进程 bge-m3 真能加载时，
   `get_vectors("st:BAAI/bge-m3")` + text_hash 命中 → **复用陈旧的 hashing 向量当 cache-hit**，
   真模型永远不会重嵌。`degraded=False`、`is_semantic=True`，**零信号**显示索引其实是 hashing 派生。

### 证据（可复现）
- `scratchpad/rag_probe4.py`（用 `FailingSemanticEmbedder` 精确复刻离线首跑路径）：
  ```
  embedder.degraded = True
  embedder.model_id = 'hashing-1024'
  VectorRetriever.model_id = 'st:BAAI/bge-m3'    ← 陈旧
  VectorRetriever.is_semantic = True             ← 撒谎
  Persisted vector model_id(s): ['st:BAAI/bge-m3']  ← 投毒 key
  ```
- `scratchpad/rag_probe5.py`（run1 降级→run2 模型可用，同一 sqlite）：
  ```
  CACHE POISONING CONFIRMED: run2 reused run1's hashing vectors as a cache hit.
  run2 embedder.degraded=False  is_semantic=True   ← 真模型从未重嵌
  ```
- 真入口确认无兜底：`pipeline/project.py:57-60` `resolve_embedder()`（auto→懒加载 SemanticEmbedder）
  → 直接 `VectorRetriever(embedder=...)`，构造后**无**降级复检。离线首跑机器上必然触发。

### 修复方向（不打补丁、治根）
让 `VectorRetriever` 不要在 __init__ 快照 `model_id`；改成属性透传 `self.embedder.model_id`，
或在 `_reindex` 用 embedder 当前 model_id 做持久化 key + `is_semantic` 读 `embedder` 的实时状态
（并消费 `degraded`）。这样 cache key 自动跟随真实后端，投毒与撒谎一起消除。
同类盲点（构造期快照 `model_id.startswith("st:")`，降级后不复检）：
`assist/contradiction.py:77,99`、`assist/sweep.py:53,202`（严重度较低：仅 init 时开关一个特性层，
但根因同）。

---

## 【已修·验证 OK】graph/community.py relay 桥接（R3 新代码，重点压测项）

R3 在 `_projection` 加了 relay 桥接（中继节点折叠成两端实体直连边）。**桥接逻辑本身正确**：

- **无自环、无虚假语义边**：`_bump`（community.py:163-167）显式 `if source == target: return`，
  且只在「同一个 relay 的真实 substantive 邻居两两之间」连边——这些实体本来就经由该 relay
  逻辑相关（如 dialogue relay 连 speaker NPC + 其 quest；qer relay 连 quest + event）。
  不是把语义无关实体硬连。证据 `rag_probe.py`：projection 131 节点/224 边，`has self-loop = False`。
- **事件实体真进社区了**：3 个有 qer 中继的事件（evt_mist_fire→c2, evt_xuanwu_pact→c5,
  evt_salt_battle→c4）proj_degree=1，**成功入社区**。另 3 个事件（evt_road_fall/ironwall_siege/
  canglang_flood）proj_degree=0 仍 singleton——但它们**内容上零边**（无 qer 引用），属
  brief 已认定的内容 gap，非投影 bug。**未误报。**
- **acceptance 世界无 O(n²) 风险**：relay 度数全 ≤2（dialogue=2, qer=2, localization=1），
  桥接总操作仅 39。

### ⚠️ 但桥接的 O(k²) 上界是真实的（边界提示，非当前 bug）
`rag_probe2.py`：构造 N 个 quest 共享**同一个** localization key（合法内容——多 quest 指向同一
翻译键是正常本地化模式，`Quest.localization_keys` 无去重/唯一约束），该 `localization:<key>` relay
就有 N 个 substantive 邻居 → 桥接产生 C(N,2) 条边：
```
n_quests=500 → projection edges=125250 ≈ C(500,2)  (O(n^2) CONFIRMED, _projection 237ms)
```
- 现状评级**低/非阻断**：当前世界 50–300 节点、relay 度数极小，触发不到。属"大世界 + 共享键
  hub"才会显现的**性能悬崖**，不是功能 bug。
- 诚实标注：这是设计的固有特性（GraphRAG relay 投影的标准代价），但代码与 docstring 都
  **没标这个上界**。若未来世界变大或出现共享 localization key 的 hub，`detect_communities`
  会变慢/吃内存。建议：要么对单 relay 邻居数设阈值（超阈值跳过桥接，记 warning），要么
  docstring 显式标注 O(Σ k_i²) 复杂度。**留作提示，不当 bug 报。**

---

## 【已修·验证 OK】text_match.py NFKC 折叠（R3 新代码，重点攻击项）

攻击点：NFKC 会不会把语义不同的字符误折叠导致召回错配？degraded 路径？

- **NFKC 折叠语义安全**（`rag_probe3.py`）：全角→ASCII（区域１２→12）、圈号①→1、连字 ﬁ→fi、
  全角冒号：→: 、半角片假名 ｱ→ア——全是**合法 Unicode 等价归一化**，提升召回，不制造错配。
- **未发现"语义不同字符被误合并"**：
  - 罗马数字 Ⅻ → NFKC → `XII`（**不是** `12`）。`lexical_score('第Ⅻ区',['第12区..'])=0.0`，
    正确地**没有**把 Ⅻ 和阿拉伯数字 12 混淆。
  - CJK 兼容表意文字（U+F900-FAD9）：豈(U+F900)→NFKC→豈(U+8C48)，**折叠到主块的等价字**。
    这是 legacy 重复字符的正确统一（Unicode 设计意图），**不是**误合并。
- **次要观察（非 bug）**：`_CJK_RE` 仍列 `豈-龎`，但 `query_terms` 先 NFKC 再跑正则
  （text_match.py:53,59），NFKC 已把这些兼容字折进主块——所以正则里这段对 `query_terms` 而言
  是"防御性死分支"。docstring 已诚实说明"Text is NFKC-normalised first"。不构成功能问题，
  仅冗余。

---

## 【验证 OK】其余检索栈正确性抽查

- **dense/reranker 真接模型，非搭壳**：`SemanticEmbedder` 真调 `SentenceTransformer.encode(
  normalize_embeddings=True)`（embedding.py:119）；`NeuralReranker` 真做 cross-encoder
  `predict([(query,passage)])`，FlagReranker/CrossEncoder 双后端 + 降级 warning
  （neural_rerank.py）。降级路径都有 WARNING 且打 `source="reranked_lexical_fallback"`（显式，非静默）。
- **bge-m3 dense 无 instruction 前缀**：embedding.py:23-46 引官方 model card `query_instruction_
  for_retrieval=N/A`。**符合官方规范，N/A，不是缺陷**（按 brief 提醒，不误报）。
- **eval 真测语义、非 BM25 名字匹配**：`retrieval_eval_queries()`（acceptance.py:548-623）查询
  **不含实体名**，按功能/关系/历史描述（"控制北方山道的武装势力"→fac_iron）；
  `run_semantic_retrieval_benchmark` 真用 SemanticEmbedder 对比 bge-m3 vs BM25-only，离线 CI
  skip 且诚实标注 n=15 样本不足以做 Wilson CI。30 条 verbatim benchmark 也诚实标注"主要测 BM25
  可靠性，非语义质量"。**口径披露到位。**
- **acceptance 改名/分母**（brief 关注项）：`qa_citation_existence_or_refuse` 改名后全仓无旧名
  残留引用；detection 分母 = `len(seeded)`（实际 25 条），`rules_covered/uncovered` 用
  `build_default_rule_registry().codes()` 实时算（acceptance.py:1022-1027），无硬编码 20/29 漂移。
  docstring 里 "~20 of 29" 是文字描述、机读用实时集合差，**未漂移**。
- budget.py（bge-m3 tokenizer 计 token，降级 len//3 有一次性 warning）、rerank.py（纯词法、
  无神经、无学习权重，docstring 诚实）、fusion RRF——均与文档一致，无搭壳。

---

## 一句话总结
检索栈整体扎实、R3 的 relay 桥接与 NFKC 两处新代码都正确（事件真进社区、NFKC 不误合并），
但发现**一个高危真 bug**：R3 新加的 embedder `degraded` 机读标记是孤儿——`VectorRetriever` 在
构造期快照 `model_id`、`is_semantic` 永远撒谎 True、且降级产出的 hashing 向量被以 `st:bge-m3`
key 持久化造成跨进程缓存投毒，离线首跑机器上必然触发静默降级，直接踩"不静默降级"红线
（复现见 rag_probe4/5.py）。
