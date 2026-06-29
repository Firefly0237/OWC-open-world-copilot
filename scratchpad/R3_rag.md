# R3 · RAG/检索栈深审（专注检索的研究生视角）

被测：commit 8cfd19b（R1+R2 已修版）。审计范围：retrieval/（embedding/rerank/neural_rerank/budget/vector/bm25/graph_expand/fusion/context_pack/community_reports）、graph/community.py、evaluation/acceptance.py 检索 eval。

结论先行：**未发现严重/高级真 bug。R2 的 RT5 三处修复均已落地且正确。检索/reranker/embedder 是真接模型不是搭壳（有可复现实证）。社区发现算法正确（modularity 0.666）。eval 真测语义检索而非名字匹配（有 leak=[] 实证）。** 仅找到 2 个「中/低」的真实但已诚实披露/有限影响的设计性缺口，外加若干验证 OK 点。

---

## 一、先确认 R2（RT5）三处修复真修了 —— 全部 ✅

1. **Leiden 权重检测 bug**（community.py:197）：旧码 `if "weight" in (g.edges[u,v] for u,v in g.edges())` ——
   `in` 作用于生成器永远 False，导致 Leiden 永不传权重。R2 改为 `if nx.get_edge_attributes(g, "weight")`。
   git show 8cfd19b 确认 diff；语义正确（generator 无 `__contains__`，会退化成逐元素 `==` 比较，
   恒为 False）。**真修了，且根因解释正确。**

2. **indirect query 改写成无实体名关系链**：retrieval_eval_queries() 的 3 条 indirect（idx 10-12）
   现在不含任何 faction/region 规范名（test_indirect_queries_contain_no_faction_canonical_names /
   _no_region_canonical_names 双向断言）。**真改了。**

3. **neural_rerank docstring**（neural_rerank.py:15）：现写 "classification-head fine-tuned
   cross-encoder"，不再误称 LoRA。**真修了。**

---

## 二、真实落地验证（红线1：真实现 vs 搭壳）—— 全部是真实现

可复现命令（.venv 已装 sentence-transformers 5.5.1，bge-m3 已缓存于
`C:\Users\10342\.cache\huggingface\hub\models--BAAI--bge-m3`）：

```
HF_HUB_OFFLINE=1 python -c "from owcopilot.evaluation.acceptance import run_semantic_retrieval_benchmark; print(run_semantic_retrieval_benchmark(tmpdir))"
```

实测结果（真跑 bge-m3 前向，非 stub）：
```
skipped: False
bge_m3_hit_rate:        0.4615
bm25_only_hit_rate:     0.2308
delta_hit_rate:        +0.2308   ← 语义 leg 真带来 +23pt 绝对提升
paraphrase_hit_rate_bge_m3: 0.60
paraphrase_hit_rate_bm25:   0.30   ← 释义查询语义路径 2x BM25
```

- **SemanticEmbedder**：真调 `SentenceTransformer.encode(normalize_embeddings=True)`，L2 归一化、
  SQLite 持久化向量（vector.py 按 (ref,model_id,text_hash) 缓存，只重嵌变更行）。非搭壳。
- **NeuralReranker**：真 cross-encoder，FlagReranker→CrossEncoder 双后端，predict 失败显式
  fallback 到 lexical 且标 `source="reranked_lexical_fallback"`（不静默）。非搭壳。
- **budget.py**：真用 bge-m3 AutoTokenizer 数 BPE token，fallback `len//3` 有一次性 warning。
- bge-m3 dense 路径不加 instruction prefix —— 确认是官方规范（query_instruction_for_retrieval=N/A），
  **不是漏 prefix**（前两轮已纠正，本轮复核同意，不误报）。

---

## 三、eval 真测语义检索而非 BM25 名字匹配 —— ✅ 有硬实证

强制 hashing/BM25-only 路径跑 13 条 answerable eval query（释义+indirect），逐条检查目标实体
规范名/别名是否被查询字面包含：

```
[para]  控制北方山道的武装势力          -> entity:fac_iron       miss  leak=[]
[para]  铁卫军团签发的通行凭证          -> entity:item_xuantie_seal miss leak=[]
[indirect] 第一行省第一个烽燧巡查任务的委托人归属于哪个势力 -> entity:fac_iron miss leak=[]
... (全 13 条 leak=[]，BM25-only 命中率 3/13 = 0.23)
```

**13 条全部 `leak=[]`** —— 没有一条把答案实体的名字偷偷塞进查询。BM25-only 只有 0.23、bge-m3
拉到 0.46，证明这套 eval 确实在测语义检索的增量，而非名字命中。**eval 诚实。**

（对照：retrieval_benchmark_queries() 的 30 条 verbatim 查询确实含实体名 —— 但代码与 docstring
都明确标注「这 30 条主要测 BM25 可靠性，不证明语义检索」，acceptance gate 用 HashingEmbedder 也
诚实声明只证 BM25 可复现。无误导。）

---

## 四、找到的真实缺口（均诚实披露/有限影响，非严重）

### 【中】事件实体被 GraphRAG 社区层永久排除（substantive-graph 建模缺口）
- **根因**：events/items/concepts 通过中继节点连接 —— `evt_salt_battle` 仅经
  `quest_event_ref:qer_03` 这个中继节点连到 quest（index.py:161-164 建 quest_event_ref→quest 和
  quest_event_ref→event 两条 reference 边）。但 community.py 的 `_projection` 只保留
  `_SUBSTANTIVE = {entity, poi, region, quest}`，**quest_event_ref 不在其中**，且 `_projection`
  要求边两端都 substantive（community.py:150 `source not in sub or target not in sub: continue`）。
  于是 event↔ref↔quest 的桥被中继节点拦腰斩断，**所有 6 个 evt_\* 在投影里 degree=0**，全部沦为
  singleton 社区，永不进任何 community report。
- **证据**：
  - `detect_communities(acceptance_world)` → 23 社区，其中 **11 个 singleton，含全部 6 个
    evt_\*、3 个 item_\*、2 个 concept_\***，投影 degree 全为 0。
  - 全图 `g.neighbors("entity:evt_salt_battle", radius=1)` = `['evt_salt_battle','quest_event_ref:qer_03']`
    —— 唯一邻居是中继节点。
- **影响**：事件本是核心叙事对象（战争/盟约/海战），却无法被宏观/holistic 问题（"这世界有哪些
  重大历史冲突、哪些任务引用它们？"）经 GraphRAG 社区摘要回答 —— event 永远不在任何社区里。
- **真 bug 还是设计选择**：**真实建模缺口**，但**不是静默降级**（没有任何虚假宣称说 event 进社区；
  community_reports 在无索引时优雅 no-op）。严重度压到「中」因为：(a) 确定性审计/行级检索仍能
  覆盖 event 的一致性（timeline、too-early-reference 规则都在跑且 detection gate 通过）；
  (b) 仅影响 GraphRAG 宏观摘要这一条增强路径。**修法（若做）**：在 `_projection` 里把
  quest_event_ref（及类似中继 reference 节点）做「透传折叠」——
  把 quest—ref—event 折叠成 quest—event 的一条加权边，而非把中继节点当 substantive。

### 【低】CJK 分词正则漏 BMP 外/兼容区/全角（仅影响 $0 离线 fallback 路径）
- **根因**：text_match.py:8 `_CJK_RE = [㐀-鿿]`，覆盖 Ext-A + 主 BMP 块，但**漏掉**：
  CJK Ext-B+（U+20000+，星平面，如 `𠀀`）、CJK 兼容表意文字（U+F900–FAFF，如 `﨎`）、全角数字。
- **证据**（query_terms 实测）：
  - `𠀀任务` → `['任务']`（`𠀀` 被丢）
  - `﨎据点` → `['据点']`（`﨎` 被丢）
  - `区域１２`（全角数字）→ `['区域']`（`１２` 被丢）
- **影响**：这些字符在 hashing/BM25-fallback 的 `query_terms`/`lexical_score` 里被静默排除，召回
  漏匹配。**但真实语义路径（bge-m3 AutoTokenizer）原生处理全部这些字符**，所以只波及离线 $0
  fallback。严重度「低」：(a) 仅 fallback 受影响；(b) 这些字符在游戏 lore 中罕见（Ext-B 多为
  生僻人名/古字，兼容区多为废弃重复字）；(c) 是召回缺口非崩溃/正确性错误。
- **真 bug 还是设计选择**：**轻微未处理边界**（非有意）。修法：正则扩为
  `[㐀-鿿豈-﫿\U00020000-\U0002ffff]` 并对全角数字 normalize（NFKC）。

---

## 五、本轮验证过、确认 OK 的点（无需改）

- **CNM 社区发现正确**：modularity=0.6663（>0.3 即显著，0.66 优秀），2 次运行结果完全一致
  （确定性 post-sort 生效），最大社区仅占 16% 节点（无"全塞一个 blob"退化），12 连通分量 →
  11 社区 +11 singleton 合理；singleton 全是 degree=0 的孤立节点，非分区 bug。
- **Leiden 路径**：opt-in（需 `pip install python-igraph leidenalg`，未默认装），缺失时
  抛带安装指引的 ImportError —— 诚实降级。权重检测已修。`_nx_name` 标签回映正确。
- **reranker 术语**：rerank.py（LexicalReScorer）反复明确「这不是 neural cross-encoder」，
  neural_rerank.py 才是真 cross-encoder，两者职责清晰、互引正确。无搭壳冒充。
- **两阶段 RAG 结构正确**：recall（bm25+vector+graph 多路）→ RRF 融合 → rerank（对 anchor query，
  语义分仅在 vector.is_semantic 时注入）→ budget 裁剪。context_pack.py:84-87 的
  `is_semantic` 守卫保证 hashing stub 下不混入伪语义分。relation-completion（:69-79）补全实体关系。
- **VectorRetriever**：exact cosine（np.argsort stable），score<=0 截断，向量按 text_hash 增量
  重嵌 + prune 删除行，model_id 隔离（`st:` vs `hashing-`）—— 工程正确。
- **fusion/budget**：RRF 标准实现；budget 用真 BPE tokenizer，trim 逻辑正确（首条必留）。
- **embedder 自动降级**：semantic 加载失败 → 一次性 warning + 退 HashingEmbedder（非静默）；
  `mode=semantic` 显式 opt-in 时 fail-loud（_ensure_model 立即加载）—— 符合 no-silent-downgrade。
- 相关测试 94 passed / 2 skipped（skip=语义 benchmark 的 CI-skip 路径，符合预期）。

---

## 六、关于 indirect graph-hop 查询的诚实说明（非 bug，但值得编排者知道）

3 条 indirect 查询（quest→giver→faction 等 2-hop）即使用 bge-m3 也 **0/3 命中**
（实测 top5 里 fac_iron 始终进不了 pack；Q12 检索到 `fac_iron:allied_with:fac_trade` 关系但
`entity:fac_iron` 节点本身没进）。**这不是隐藏 bug，也不是不诚实** —— benchmark 把 indirect 计入
分母并如实返回偏低的 delta，没有任何 gate 假装 indirect 通过。但实质是：检索 pipeline 做的是
单跳邻居扩展（从名字命中的 seed 做 radius-2），而 indirect 查询故意不含 seed 名 → graph 扩展拿不到
有效锚点（或锚到过多噪声 seed），所以 query 要求的"先定位 FIRST 区的烽燧任务→其委托人→该委托人
所属势力"这种多跳推理，检索层本就不做。**这是 GraphRAG「indirect 类」目标值偏高的已知现实，属
v1 增强方向，符合 brief「已知 v1 增强方向」豁免**，列此仅供编排者对 delta 数字有正确预期。

---

一句话总结：RAG 检索栈是真接模型的真实现（bge-m3 +0.23 命中率实测、reranker/tokenizer 真跑），R2 三处 RT5 修复全部确认落地正确，eval 经 leak=[] 实证确为语义测试非名字匹配；仅发现 1 个「中」（事件实体因 quest_event_ref 中继节点被社区投影过滤而永久排除在 GraphRAG 社区外）+ 1 个「低」（CJK 正则漏 BMP 外/兼容/全角，仅累及 $0 fallback）真实缺口，二者均已诚实披露、无严重/高级问题、无静默降级。
