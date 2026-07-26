# Live evaluation report — 真实 LLM / 真实向量的质量数字

- 模型（judge）：`deepseek-v4-flash` · 日期：2026-07-03
- 复跑：`python scripts/run_live_eval.py`
- 定位：$0 确定性套件证接线/逻辑；本报告测 **live 侧**。两者互补，不互替。

## 1 · 端到端 live QA 质量（真实模型回答 + 真实模型 judge）— 主指标

- 问题：**6**（可回答 4 + 不可回答 2）
- **live faithfulness = 1.0**（claims 5，不被证据支持 0）
- 不可回答题正确拒答：**2/2**
- 成本：$0.0058

## 1b · 对照组：离线拼接式答案 + live judge（测的是 $0 替身的答案风格，非 live 质量）

- 判定答案数：4 · claims：50 · 不被证据支持：10
- faithfulness = 0.8（离线替身把 ~12 行检索结果整体拼进答案，弱相关行被保守 judge 拒——此组恰好证明该门有判别力、非永真）
- 注：此组跨 run 观测到 0.58–0.8 的方差（单 LLM judge 非确定性）；端到端主指标以拒答行为+短答案为主，方差远小。
- judge 成本：$0.0176

## 2 · 幻觉活体探针（军歌 case：in-canon 实体 + out-of-canon 事实）

- 编造答案被抓：**是**（faithfulness=0.0）
- 有据对照答案放行：**是**（faithfulness=1.0）
- 截至此步累计 judge 成本：$0.0179

## 3 · 检索消融（真实 bge-m3 hybrid vs BM25-only，paraphrase/间接问法查询集）

- 查询数：**13**
- hybrid（bge-m3）hit-rate：**0.4615** · BM25-only：**0.2308** · **语义增益 delta = 0.2308**
- 仅 paraphrase 子集：bge-m3 **0.6** vs BM25 **0.3**
- 注记：n=13 answerable queries (paraphrase + indirect). Sample size is too small for Wilson CI < ±5% (need n>=100 for that). Verbatim queries (entity-name based) are omitted here -- they primarily test BM25 reliability, not semantic retrieval quality. HashingEmbedder pin in acceptance_gate: demonstrates BM25 reproducibility, not bge-m3 semantic recall. Semantic uplift (delta): +23.1% on paraphrase queries.

---
诚实边界：样本量为验收集规模（非统计大样本）；judge 为单模型（无多 judge 一致性）；
数字用于证明 live 链路质量可测可复算，不用于对外宣称统计显著。