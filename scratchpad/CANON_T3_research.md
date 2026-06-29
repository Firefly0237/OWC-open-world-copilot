# Team 3 设计（编排者补，原调研 agent 超时）— evaluation/ + qa/

## P2-b — opt-in、$0-可跳过的 LLM-judge faithfulness 评测
**已被预祝福**：`qa/verify.py` 模块 docstring 原文已写：「If an entailment backend is ever added, it belongs in a **separate, opt-in verifier** — do not quietly upgrade this function's promise.」→ 我们正是加这个独立 verifier，不动 `verify_qa_answer`（citation-existence）。

**canon（RAGAS faithfulness）**：把答案拆成若干断言(claim)，逐条让 LLM judge 判断「该断言是否被检索到的证据(pack 里的 hit 文本)蕴含/支撑」，输出 supported/unsupported；faithfulness = supported / total。

**落地**：
1. 新函数（建议放 `qa/` 旁，如 `qa/faithfulness.py`），签名类似：
   `def judge_qa_faithfulness(answer: QAAnswer, *, pack: ContextPack, judge: LLMGateway | None = None) -> dict`
   - judge=None 或探测不可用（离线/无 key）→ return `{"skipped": True, "reason": "..."}`，**不报错、不破 $0**（镜像 `evaluation/acceptance.py` 的 `run_semantic_retrieval_benchmark(skip_if_no_semantic=True)` 跳过范式——先读它照抄探测+返回结构）。
   - judge 可用 → 对每个 claim×证据 调 judge（gateway.complete，task 标签如 "faithfulness_judge"），解析 supported/unsupported，返回 `{"skipped": False, "faithfulness": float, "claims": [...], "unsupported": [...]}`。
2. judge prompt 草案：给定【问题】【答案中的一个断言】【检索到的证据文本】，要求**只输出** JSON `{"supported": true/false, "reason": "..."}`，明确「supported=证据是否真的蕴含该断言，存在但不相关=false」。解析失败 fail-closed（当 unsupported 或标 parse_error，不静默当 supported）。
3. **与现有门并存不替换**：在 `evaluation/acceptance.py` 加一个**可跳过**的 faithfulness gate（默认离线 skipped=True，不计入失败；有 judge 时纳入），与 `qa_citation_existence_or_refuse` 门并列。明确命名 `qa_faithfulness_entailment`（区别于 existence）。
4. 诚实：这是**新增能力**非 bug 修复；不引入必装依赖；$0 默认行为不变（默认跳过）。

**测试**（都不花钱）：
- 跳过路径：judge=None → skipped=True + reason（mirror semantic benchmark 的 skip 测试）。
- mock judge 判定正确性：注入一个返回固定 JSON 的假 gateway，构造「答案断言被证据支撑」→ supported；「实体在 canon 但具体事实不在证据」(正是 verify.py docstring 举的 军歌 例子)→ unsupported，faithfulness<1。
- 解析失败 fail-closed 测试。

## 领地与并发
本队独占 evaluation/ + qa/（新增 qa/faithfulness.py + 改 acceptance.py 加 gate）。Team1=content/、Team2=llm/+agent/react.py，不重叠。注意 acceptance.py：Team 2/Team 1 不碰它，安全。
