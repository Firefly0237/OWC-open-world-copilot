# Prompt A/B — agentic follow-up rewriter，迭代前 vs 迭代后（含方法论实录）

- 模型：`deepseek-v4-flash` · 日期：2026-07-03 · 复跑：`python scripts/prompt_ab_followup.py`
- 迭代由来（两处真实失败，live 调试发现）：①v1 引用证据 ref 里的内部 id（如 `fac_sand`）作查询——id 不在可检索正文，桥接落空；②v1 的裸 array 输出契约在 provider json_mode（强制 object）下非确定性解析为空 → 静默降级到启发式。v2 = 强制显示名 + object 契约；降级现已必留日志。

| 臂 | 真多跳桥接 | 疑似兜底轮 | 成本 |
|----|-----------|-----------|------|
| v1（迭代前） | 0/0（真多跳题 0/4） | 0/4 | $0.0025 |
| v2（迭代后） | 0/0（真多跳题 0/4） | 0/4 | $0.0047 |

## 诚实结论

1. **命中率维度无区分度**：单轮对照（single_hit）显示本语料上 4 题首轮全命中——hybrid+graph+每跳保底的检索基建在小语料上已单轮足够，live 命中率无法区分两版 prompt。构造出'单轮必 miss 且 v1 必败'的语料在小规模下不可得，如实呈现而非硬凑。
2. **真实收益在效率与鲁棒性**：v2 的后续查询干净、语言正确（如'黑沙帮 据点'），v1 曾出现 id 查询与兜底空转轮；object 契约消除了 json_mode 下的非确定失败面。
3. **本实验最大的产出是它连环倒逼出的架构修复**（全部已固化+单测）：
   - json_mode×array 契约冲突 → object 契约 + 双形态解析；
   - 静默降级 → fallback 必留日志（no silent downgrade 自我执法）；
   - 锚定重排在小预算下挤出跨跳证据 → IRCoT 式每跳保底；
   - LLM 空回复被误当失败 → 识别为精判充分信号（两级充分性，stop_reason=llm_no_gap）。

局限：n=4、单模型单 run；多跳增益的判定性证明见 $0 单测（tests/test_agentic_retrieval.py，单轮 miss → agentic 捞到）。注册表 `retrieval.agentic.followup` version=2 与本迭代对应。