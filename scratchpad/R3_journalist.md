# R3 — Harness 成熟度复测（资深 agent-harness 记者视角）

被测：OWCopilot，分支 `feature/agent-pipeline-enhancements`，聚焦 harness 四件套
（上下文压缩 / OTEL 可观测 / tokenizer / lesson archive）。

前提：上一轮已认定 harness 是真升级、压缩/可观测/tokenizer 评分高。本轮**不复述该结论**，
只找新的、具体的、可复现的「宣传 vs 实现」差距。结论：**确实成熟，但找到 2 个真实的
小缺陷 + 1 个对标差距**，全部给了 file:line，且都属「低/中」级（确定性闸门与人审兜底仍在）。

---

## 【中】1. OTEL `gen_ai.request.model` 记的是 task 标签，不是真实模型 —— 违反 GenAI 语义约定

**根因（非表象）**：react.py 在创建 `gen_ai.chat` span 时把 `self.task` 当成模型名传入，
而 `self.task` 默认是 `"agent_react"`（任务标签），不是模型。

- 调用点：`src/owcopilot/agent/react.py:185`
  ```python
  with gen_ai_chat_span(tracer, model=self.task, step_idx=step_idx) as chat_span:
  ```
  `self.task` 来自构造函数默认 `task: str = "agent_react"`（react.py:118）。
- span 把它写进 `gen_ai.request.model`：`src/owcopilot/llm/otel_bridge.py:401`
  ```python
  "gen_ai.request.model": model,   # ← 实际收到 "agent_react"
  ```
- **真实模型名其实唾手可得**：gateway 在算缓存键时已经解析出来了——
  `src/owcopilot/llm/gateway.py:213` `model = getattr(provider, "model", None) or tier`
  → `deepseek-v4-flash` / `deepseek-v4-pro`（gateway.py:72-73）。span 拿不到只是因为
  react.py 没去取，传错了字段。
- react.py 全文没有任何地方事后回填正确的 `gen_ai.request.model`
  （grep `request\.model|set_attribute.*model` 在 react.py 零命中）。

**对标**：OTEL Semantic Conventions for GenAI 明确要求 `gen_ai.request.model` =
请求的模型标识（如 `gpt-4`、`deepseek-v4-pro`）。这里写成内部任务名，会让任何下游
（Jaeger/Tempo/Grafana 的 "model" 维度聚合、成本归因看板）按 `"agent_react"` 分组，
模型维度直接失真。属于**真实的规范合规缺陷**，不是噱头但也不是已知 v1 方向。

**为什么没被测出来**：单测 `tests/test_t4_otel_bridge.py:192,203` 是**直接**给
`gen_ai_chat_span(model="deepseek-v4-flash")` 再断言 `== "deepseek-v4-flash"`——
测的是 context manager 本身，**绕过了 react.py 的真实调用点**。没有任何端到端测试断言
一次真实 `agent.run()` 后 chat span 的 model 值。测试反而制造了「span 携带真实模型」的
假象。

**是真 bug 还是已兜底**：真 bug（规范层）。但严重度限「中」：
- token 回填（input/output_tokens）是对的（react.py:193 `_update_chat_span_tokens`
  取 `records[-1]`，在正常流程下确为本次 planning 调用，含 cache-hit 时也是 0/0 的诚实记录）；
- OTEL 默认关闭、属可观测附加面，不影响 agent 主流程正确性。
- 修复成本极低：把 gateway 解析出的 `model` 暴露到 telemetry/CallRecord 或返回值，
  react.py 回填到 chat span 即可。

---

## 【低】2. critic-side lesson 改写不完整：general 维度的「生成」措辞泄漏进 critic prompt

**根因**：`build_critic_lesson_block` 只把**一种**生成侧措辞替换成评判侧措辞，
而 `extract_lessons_from_report` 对 general 维度产出的是**另一种**措辞，replace 漏网。

- 改写逻辑：`src/owcopilot/assist/lessons.py:121`
  ```python
  text = lesson["lesson_text"].replace("生成时请着重提高", "评判时请着重核查")
  ```
  只命中 dimension≠general 的模板（lessons.py:62 `生成时请着重提高「{dimension}」维度...`）。
- 但 general 维度模板写的是另一句：`src/owcopilot/assist/lessons.py:67`
  ```python
  "生成时请整体提高质量标准，不要依赖 critic 的宽松判断。"
  ```
  其中**不含** `生成时请着重提高` 子串 → replace 不触发 → critic prompt 里原样出现
  「**生成**时请整体提高质量标准」。在 critic（评判）上下文里这是语义错位：让评审者去「生成」。

**可复现**：构造一条 `dimension="general"`、`lesson_text` 来自 `extract_lessons_from_report`
general 分支的 lesson，喂给 `build_critic_lesson_block`，输出里仍含 `生成时请整体提高`。

**为什么没被测出来**：BE-4 测试 `tests/test_be_fixes.py:253-269` 只喂了
**dimension-specific** 文案（`生成时请着重提高`），断言它被改掉。**没有任何测试**把
`extract_lessons_from_report` 真正产出的 **general** 文案灌进 critic block。
所以这条泄漏一直在测试盲区里。

**是真 bug 还是已兜底**：真 bug，但严重度「低」：
- lesson 只是注入信号、不写 canon；确定性一致性审计 + 人审是唯一落库路径，仍兜底；
- 措辞错位只轻微稀释 critic 注意力，不会造成误通过。
- 修复极简：要么 replace 串列表里补上 `("生成时请整体提高","评判时请着重核查")`，
  要么 general 模板也改成 dimension 模板同款句式。

---

## 【低】3. `agent.run_id` span 属性在生产路径里是死代码

**根因**：`invoke_agent_span` 支持 `run_id` 参数并据此设 `agent.run_id` 属性
（otel_bridge.py:355,374-375），但 react.py 的唯一调用点没传。

- 调用点：`src/owcopilot/agent/react.py:168`
  ```python
  with invoke_agent_span(tracer, agent_name=self.agent_name, goal=goal) as root_span:
  ```
  缺 `run_id=...` → `agent.run_id` 属性永不写。
- 实际查询不受影响：SQLite exporter 用 `run_id = trace_id_hex`（otel_bridge.py:168），
  `query_by_run_id` 按 trace_id 查得到。所以这只是文档化的 span 属性在生产里悬空。

**是真 bug 还是设计**：低优。功能上 trace_id 已能当 run_id 用，但 otel_bridge.py:363
的 docstring 把 `agent.run_id` 列为标准属性、实际生产 span 上没有，属「文档承诺 > 实现」的
小落差。

---

## 复测确认 OK 的点（验收信号，非问题）

- **tokenizer 降级诚实**：tiktoken 缺失/损坏都 `warnings.warn` + 回退 `len//4`，
  非空文本最小返回 1（tokenizer.py:60-77,95-96），never-crash 契约成立。
  - 唯一可商榷处是 docstring 把 cl100k_base 称为「DeepSeek V4-compatible」(tokenizer.py:43)，
    这是近似声明（DeepSeek 自有 BPE，cl100k 只是合理代理）。但代码并未据此谎称精确，且
    本就是 $0 离线预算估算用途，**不算缺陷**，仅记录。
- **压缩诚实**：从不静默丢 turn——压成显式 `[Summary of turns ...]` 标记
  (context_compressor.py:213)；checkpoint（Final Answer/Goal/Error/已压缩标记）永不进压缩批
  (context_compressor.py:65-70,174-178)；gateway 失败优雅回退 `triggered=False`
  (context_compressor.py:200-208)；硬 trim 也带 `[N earlier turns omitted]` 标记
  (react.py:417)。**符合「不静默降级」红线**。
  - 观察（非 bug）：summary 标记的「turns 1-N」里 N 是**可压缩子集计数**、非真实 transcript
    序号；多轮后多个 summary 标记会累积、不再二次合并（它们是 checkpoint），靠硬 trim 兜底。
    这是合理的 v1 行为，且丢弃有 omitted 标记、非静默——不报。
- **OTEL no-op 路径**：默认关闭时返回 `_NoOpTracer`，span CM 全无副作用、不 import OTEL
  (otel_bridge.py:543-551, 509-540)，byte-identical 契约成立。错误状态在 SkillError 与裸
  Exception 上都正确置 ERROR + record_exception（react.py:250-266，有端到端测试 593-646）。
- **lesson 防污染**：min_false_pass=3 阈值、inject cap=3、90 天降权不删、可全关、纯模板无 LLM
  (lessons.py 头注 + sqlite.py:544-587 的排序/LIMIT)，与宣传一致。
- **token 回填语义**：`_update_chat_span_tokens` 取 `records[-1]`，在「先压缩(compact 记录)
  → 后 planning(agent_react 记录)」的顺序下，`[-1]` 始终是本次 planning 调用，cache-hit 也记
  0/0，**没有错位**——这点比预期更稳，确认 OK。

---

## 一句话对标结论

Harness 主体确属真升级（压缩/可观测/tokenizer/lesson 都落地且降级诚实）；本轮新发现是
**OTEL `gen_ai.request.model` 误填 task 标签（中）**——明显违反 GenAI 语义约定且被「直接测
context manager」的单测掩盖，外加 **critic-lesson general 措辞改写漏网（低）** 和
**`agent.run_id` 属性生产悬空（低）**，三者皆有 file:line、皆被现有测试盲区漏过，但都不动
agent 主流程正确性、有确定性闸门兜底。
