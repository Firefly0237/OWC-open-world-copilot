# Team 2 设计（编排者补，原调研 agent 超时）— llm/ + agent/react.py

## P1 — gen_ai.response.model（canon-correct 做法）
**关键洞察**：OTEL 区分 `gen_ai.request.model`(你请求的) vs `gen_ai.response.model`(实际应答的)。canon 正解=**response.model 取自 API 响应体的 `model` 字段**，而非 provider 配置。这样容灾切到 secondary 时，secondary 的响应体自带 secondary 的 model → 天然正确，无需 FailoverProvider 猜「谁应答了」。
**落地**：
1. `OpenAICompatProvider.complete()` 解析响应 JSON 的 `model` 字段，随返回 tuple 带出（看 gateway.py 现有 complete() 返回结构，增量加一个 response_model）。
2. `telemetry.CallRecord` 加 `response_model: str | None`（R4 已加 `model`=request 侧）；gateway 记录时填入。
3. `agent/react.py` 的 `_update_chat_span_from_telemetry` 把 `records[-1].response_model` 写进 span 的 `gen_ai.response.model`（**仅当非空**——离线 fake 无响应 model 则不设该属性，绝不填错值）。
4. 容灾路径：FailoverProvider/CircuitBreaker 的 complete() 直接透传内层返回的 tuple（含 response_model），无需改 wrapper（response_model 随实际应答的内层流出）。
**测试**：mock provider 返回带 `model="deepseek-x"` 的响应 → 断言 span `gen_ai.response.model`==该值；failover 到 secondary → response.model==secondary 的 model（≠ request.model=primary）；离线 fake → 不设该属性。
**注意**：request.model 现状（FailoverProvider.model=primary）保持不变，是文档化设计；本项只新增 response.model，不动 request 侧。

## P2-a — ReAct 原生 tool-calling（opt-in，text 默认；必须真实现非 stub）
**复杂度诚实评估**：中等。难点=① OpenAICompatProvider 要支持 `tools` 参数 + 解析响应的 `tool_calls`；② react.py 要加一条结构化循环分支；③ 真 function-calling 路径只能用 mock provider 在 $0 下测（真 DeepSeek 路径需联网）。这是标准做法（agent 框架都用 mock LLM 测 tool 循环），属真实代码非搭壳，但要**明确文档化 live 路径需联网的 function-calling provider**。
**落地（最小真实子集）**：
1. 给 LLMProvider 协议加可选 `supports_tools` 能力位 + `complete_with_tools(system, messages, tools)`（OpenAICompatProvider 真实现 OpenAI tools/tool_calls API；fake/不支持者不实现→探测为 False）。
2. `ReActAgent` 加 `use_native_tools: bool = False`（**默认 False=文本路径，行为零改变**）。开启且 provider 探测支持时走结构化循环：发 tools → 解析 tool_calls → 经 registry 执行（复用现有 skill 执行 + AgentStep.result）→ 把 tool 结果作为 tool message 回喂 → 迭代到 finish；否则**回退文本 ReAct**。
3. 结构化与文本两条路径共用 registry 执行、AgentStep 记录、OTEL span（execute_tool_span）——只换"模型如何表达动作"。
**测试**：mock provider 返回结构化 tool_calls → 断言结构化循环正确执行工具、回喂、终止；provider 不支持 → 断言回退文本路径；默认 use_native_tools=False → 断言与现状逐字节一致（零回归）。
**红线**：默认路径零改变；结构化路径必须真实现 OpenAI tools 契约（非占位）；若执行中发现一次做不安全，交付"能力探测+接口+回退"骨架并**显式报告**留 flag，不假装完成。

## P3-a — tokenizer 近似（小改，诚实标注为主）
`llm/tokenizer.py` LLM 侧用 tiktoken cl100k 近似 DeepSeek（`retrieval/budget.py` 检索侧已用 bge-m3 真 tokenizer）。
**落地**：保留 tiktoken（DeepSeek 无公开本地 tokenizer 时这是合理近似），在 `count_tokens`/模块 docstring 明确标注「对非 OpenAI 模型为近似、误差有界，仅用于预算控制非计费」。若发现目标模型暴露真 tokenizer 则优先用之。诚实标注优先，别假精确。

## 领地与并发
本队独占 llm/ + agent/react.py。Team1=content/、Team3=evaluation/+qa/，不重叠。
