# R4 · 竞品资深 PM 视角复盘（找自欺/注水/伪闭环）

被测：OWCopilot，分支 feature/agent-pipeline-enhancements。本轮重点压测 R3「诚实化」修复是否真到位 + 新代码回归。

结论先行：R3 的几处诚实标注（QA gate 改名 citation-existence、detection 20/29 披露、normalize 守卫）**核对属实、没注水**。但发现 **1 个真 bug（中-高）**——R3 新增的 embedder `degraded` 机读标记是「只写不读」，且 degrade 路径本身让 `is_semantic` 对管线撒谎并污染向量缓存。另有 2 处低severity文档/命名漂移。

---

## 【中-高 · 真 bug】embedder degrade 路径：`is_semantic` 对管线撒谎 + 向量缓存被污染，且 R3 的 `degraded` 机读标记全程无人读

**根因（两个耦合问题）：**

1. **`VectorRetriever.model_id` 在 `__init__` 处缓存，degrade 后不更新。**
   `src/owcopilot/retrieval/vector.py:71` 在构造时 `self.model_id = self.embedder.model_id`（此刻 SemanticEmbedder 尚未懒加载，model_id 仍是 `st:bge-m3`）。`is_semantic`（vector.py:80-82）读的是这个**缓存副本** `self.model_id.startswith("st:")`，**不是** embedder 的实时 model_id。
   而 `_reindex`（vector.py:126-158）在同一个 `__init__` 里就调用 `embedder.embed_many(...)` 嵌入语料——若 bge-m3 加载失败（auto 模式 + 首次离线），embedder 当场 degrade：实时 `model_id` 翻成 `hashing-1024`、`degraded=True`，返回的是 hashing-stub 向量。但 `VectorRetriever.model_id` 已经定格在 `st:...`。

2. **R3 新增的 `embedder.degraded` 标记，全代码库无人消费。**
   `embedding.py:81-87` 的 docstring 明确承诺「context pack builder / QA layer / telemetry 可读它判断结果是 BM25-only 而非语义」。但 `grep -rn '\.degraded' src/` 显示：除了 `test_embedding_degrade_marker.py`，**没有任何产品代码读 `SemanticEmbedder.degraded`**。管线判语义与否一律走 `is_semantic`（即上面那个会撒谎的缓存 model_id），从不看 `degraded`。

**复现（已实跑确认，full chain）：**
```python
from owcopilot.retrieval.embedding import SemanticEmbedder
from owcopilot.retrieval.vector import VectorRetriever, _Row
from owcopilot.storage.sqlite import SQLiteStore
store = SQLiteStore(".../t.sqlite")
rows = lambda _s: [_Row('entity:a','npc','Aldric','caravan master')]
emb = SemanticEmbedder('definitely/not-a-real-model-xyz')   # 注定加载失败 -> degrade
vi = VectorRetriever(store, embedder=emb, rows_loader=rows) # __init__ 内 _reindex 嵌入 -> degrade 发生
# 实际输出：
#   embedder.degraded               = True
#   embedder live model_id          = hashing-1024
#   VectorRetriever.model_id cached = st:definitely/not-a-real-model-xyz
#   VectorRetriever.is_semantic     = True   <-- 管线被骗：把 hashing 向量当语义向量
#   persisted vector cache keys     = ['st:definitely/not-a-real-model-xyz']  <-- hashing 向量被存进 st: 缓存键
```

**两个具体危害：**
- **`is_semantic` 对下游撒谎**：`context_pack.py:85` 与 `inspiration/retrieval.py:48` 都用 `vector.is_semantic` 决定是否把 semantic_scores 喂给 rerank。degrade 后它们拿 hashing-stub 相似度当真 bge-m3 分数喂进重排——静默质量下降，正是 R3 那个 `degraded` 标记本应暴露的场景。
- **持久缓存中毒**：hashing 向量被 upsert 进 `content_vectors` 表、键为 `st:bge-m3`（vector.py:154 用缓存的 `self.model_id`）。下次进程 bge-m3 加载成功时，`_reindex`（vector.py:135,142）按 `st:bge-m3` 键命中这些 text-hash 相同的**旧 hashing 向量**直接复用，不再重嵌——**环境修好后检索仍静默 degraded**，直到内容文本变更才会失效。

**真 bug vs 设计**：真 bug。这不是「离线 $0 跳过语义=诚实降级」那条已定的设计——那条要求降级**有 warning + 机读标记**。warning 有（embedding.py:105），但机读标记写了没人读，而真正驱动管线的 `is_semantic` 反而报 True。等于「诚实降级」的承诺在最关键的 in-process 路径上没兑现。

**触发面诚实说明**：仅在 auto 模式 + sentence_transformers 已装 + 模型加载失败（典型=首次离线运行 bge-m3 未缓存）时发生。`semantic` 显式模式是 fail-loud（embedding.py:166-170），不受影响；纯 `hashing` 模式 model_id 本就不是 `st:`，也不受影响。所以不是天天爆，但正是作品集 demo「首次拉起、网络没配好」最可能撞上的场景，且一旦中毒会持久。

**建议方向（不替你打补丁）**：`is_semantic` 改读 embedder 实时状态（或 degrade 时回写 `VectorRetriever.model_id`）；`_reindex` 用 degrade 后的真实 model_id 作缓存键；`ProjectContext.open` 在 reindex 后检查 `getattr(embedder,"degraded",False)` 并向 API/telemetry 暴露——让那个已经写好的 `degraded` 标记真的有消费者。

---

## 【低 · 文档漂移，非自欺】README 说 eval-acceptance「verifies five gates」，实际报告有 7 个 check

`README.md:115` 写「verifies five gates (zero false positives ... 25/25 seeded errors caught, 100% impact recall, 30/30 retrieval hits, grounded-or-refuse Q&A)」。实跑 `run_acceptance_evaluation` 返回 **7 个 check**：clean_world_zero_false_positives / impact_recall_100 / retrieval_hit_rate_gate / retrieval_tight_hit_rate_gate / qa_citation_existence_or_refuse / seeded_error_detection_gate / tool_selection_accuracy_gate。

**判定**：低severity，**且是安全方向**——README **少报**（5<7），不是夸大。不构成「宣称>实现」的注水，只是文档没跟上新增的 tight-budget 重排门与 tool-selection 门。建议顺手把「five」改成实际数或泛化措辞（CLI help 文案 main.py:419-421 已经是泛化的「impact recall, 30-query retrieval and QA gates」，没踩这个坑）。

## 【低 · 命名一致性，非 bug】golden.py 的 check 仍叫 `qa_grounded`，未跟 R3 的 citation-existence 改名对齐

R3 把 acceptance.py 的 QA 门改名为 `qa_citation_existence_or_refuse` 并加了 entailment 免责。但 `src/owcopilot/evaluation/golden.py:116` 的同类 check 仍叫 `qa_grounded`（断言只是 `not refused and bool(citations)`，即引用存在性）。

**判定**：不是 bug、不是过度承诺。golden.py 是单实体（Aldric）冒烟测试，不是头牌 benchmark；且本代码库里「grounded」一词全程定义为 citation-existence（README/qa/verify.py 一致），所以名字不构成「能验事实正确」的误导。仅是改名没扫干净的命名不一致，可选清理。

---

## 验证 OK 的点（这些 R3 诚实化是真到位的，没注水）

- **detection 25/25 + 20/29 披露属实**：实跑 `detection_rate=1.0`、`detected=25/25`、`rules_covered=20`、`rules_total=29`，与 brief 所述一致。`rules_uncovered` 明列且 metrics/check.details 双重暴露（acceptance.py:1022-1043），test_acceptance_eval.py:65-82 钉死。README「25/25 seeded errors caught」准确。
- **QA gate 改名 + entailment 免责真做了**：acceptance.py 模块 docstring、`qa_citation_existence_or_refuse` 的 details.scope（acceptance.py:979-984）、qa/verify.py 模块 docstring（1-24 行）全都明写「只验引用存在性，不验 entailment，in-canon entity/out-of-canon fact 幻觉是已知 untested gap」。前端 AskPage.vue 文案「答案附依据，查不到会直说」与实现相符，未宣称验事实。**唯一边界措辞**：README:61「refuses unsupported claims」严格读可被理解成 entailment，但实现确实会对「检索无依据」的 claim 拒答，可辩护——不升级为 bug，仅提示。
- **多智能体确定性验证降级安全**：verifier.py `_deterministic_verify`（200-225）对 audit 缺失/SkillError/通用异常/非 int（含 bool 子类型陷阱，222 行）全部安全回退到 LLM-parse，再回退到诚实 `needs_more`，无伪造 pass。`_compute_verdict` 的 ±1 容差是显式文档化的（279-291），可讨论但非自欺。
- **ScopedSkillRegistry 是真执行期拦截**：skill_scope.py `run()` deny-by-default（42-47），`__contains__` 取 allowed∩base 交集，`manifest` 交集防广告越权工具。verifier.py:211 的 `in` 检查与代理兼容。非搭壳。
- **normalize.py id/locale 守卫无误拒**：`_resolve_id` 对显式 id 走严格规则、合成 id 仅放行结构冒号且仍查 traversal/控制字符/长度；显式带冒号 id 仍被拒（130-133），合成冒号不可被用户滥用逃逸目录。locale 白名单含 zh/en/id 等合法 2 字母码、且用 `_LOCALE_RESERVED_KEYS` 挡掉「id」误判为印尼语的老坑（606-674）。边界正确。

---

**一句话总结**：R3 标的诚实化（25/25 与 20/29 披露、QA 改名 citation-existence、确定性验证/工具沙箱/id 守卫）核对全部属实没注水；但揪出一个真 bug——R3 新加的 embedder `degraded` 机读标记在产品里没有任何消费者，而真正驱动管线的 `is_semantic` 因 model_id 在构造期定格，会在 bge-m3 加载失败时对管线谎报 True 并把 hashing 向量污染进 `st:` 语义缓存键（持久），等于「诚实降级」承诺在最关键的离线首启路径上没兑现。
