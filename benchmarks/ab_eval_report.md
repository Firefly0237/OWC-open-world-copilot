# 检索配置 A/B — 配对 McNemar 显著性检验（替代旧 4 题 A/B）

- 日期：2026-07-13 · 复跑：`python scripts/run_ab_eval.py`（离线确定性 $0）
- 数据集：`v1`（hash `cd444e50fb766b1f…`）· embedder=`HashingEmbedder` · 语料节点数=364
- **取代说明**：本报告取代 `benchmarks/prompt_ab_report.md`——旧 A/B 仅 4 题、真多跳桥接分母为 0/0（无可检验样本），被外部验收判为无效；本报告改用 n=104 配对样本 + McNemar 精确检验 + 多跳准入门禁。（旧文件保留为 prompt v1/v2 迭代的工程实录，其统计结论以本报告为准。）

## A/B 两臂（生产 ContextPackBuilder 上的一行配置切换）

- **臂 A `bm25_only`**：稀疏词法召回 + 融合，无向量腿、无图扩展、rerank 关。
- **臂 B `hybrid_rerank`**：混合召回（bm25 + 向量 + 图扩展）+ rerank 精排阶段。
- 两臂复用同一 project 的检索器（与 `qa_context_builder` 同法），仅构造参数不同；逐题二元判据 = success@k（gold ref 是否进入 top-k）。

## 多跳准入门禁（把『题不可答』与『检索没找到』分开）

- 多跳（indirect）题共 **24** 道；逐跳核对 gold 证据链是否落在索引语料内。
- **准入 24 / 拒收 0**。
- 无拒收：全部多跳题的每一跳 gold 证据都在语料内且可被引用——故 indirect 上的低 recall 是真实检索难度（多跳单遍召回难），而非题目不可答。

## 主对比（预注册操作点 k=1，success@1）

配对样本 n = **104**（k=1）。2x2 列联表：

| | B 命中 | B 未命中 | 行计 |
|---|---|---|---|
| **A 命中** | 49 | 5 | 54 |
| **A 未命中** | 9 | 41 | 50 |
| 列计 | 58 | 46 | 104 |

- 逐题命中率：A(bm25_only) **0.5192** · B(hybrid_rerank) **0.5577**
- 不一致对儿：b（A对B错）= **5** · c（A错B对）= **9** · 合计 n_discordant = 14
- McNemar 精确二项 p 值 = **0.42395**（n_discordant<25，不报卡方近似，仅用精确检验）
- 结论（克制）：差异在 α=0.05 下**不显著**（p=0.42395）；观测方向偏向 B（hybrid_rerank），但不一致对儿不足以支撑显著性结论。

## 全 k 扫描（透明起见，逐 k 报，不挑点）

| k | A 命中率 | B 命中率 | b | c | McNemar p | 显著(α=0.05) |
|---|---|---|---|---|---|---|
| 1 | 0.5192 | 0.5577 | 5 | 9 | 0.42395 | 否 |
| 3 | 0.5962 | 0.6058 | 4 | 5 | 1.0 | 否 |
| 5 | 0.6442 | 0.6731 | 5 | 8 | 0.581055 | 否 |
| 10 | 0.7019 | 0.7019 | 5 | 5 | 1.0 | 否 |

## 分类目分解（k=1）

| 类目 | n | A 命中率 | B 命中率 | b | c | McNemar p |
|---|---|---|---|---|---|---|
| verbatim | 30 | 0.8667 | 1.0 | 0 | 4 | 0.125 |
| paraphrase | 50 | 0.5 | 0.56 | 2 | 5 | 0.453125 |
| indirect | 24 | 0.125 | 0.0 | 3 | 0 | 0.25 |

---
诚实边界：Paired success@k over the frozen eval set (embedder=HashingEmbedder); McNemar exact binomial p-values. Under HashingEmbedder the absolute hit-rates are not a semantic-quality claim (methodology demo); inject a semantic embedder for a live number.
McNemar 精确检验的教科书校验值钉在 `tests/test_ab_compare.py`（(5,15)→p≈0.0414；(0,8)→0.0078125；b=c→1.0；b+c=0→1.0）。