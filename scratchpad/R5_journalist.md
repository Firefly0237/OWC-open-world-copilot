# R5 (末轮) — Harness 成熟度复测 + sign-off（资深 agent-harness 记者视角）

被测：OWCopilot，分支 `feature/agent-pipeline-enhancements`，HEAD=R4 fixes（5 组）。
聚焦 harness：上下文压缩 / OTEL 可观测 / token 预算·tokenizer / 记忆·lesson / tool 注册调用。
本轮双线：(1) 验证 R4 对我上轮【中】号缺陷的修复是否到位、是否引入新细微落差；(2) 末轮总评 sign-off。

**结论先行**：R4 的 `.model` 透传修复**方向对、覆盖了我 R3/R4 报的主症状（容灾包装路径不再退回 tier 标签）**。
但正如简报预判的，透传取的是 **primary/inner**，于是出现一个**新的、更细的真实落差**：
**运行时一旦真正 failover 到 backup，`gen_ai.request.model` 与缓存键报的仍是 primary，而非实际产出响应的 backup。**
这是【低-中】、可复现、且 OTEL 语义约定上确有依据的残留缺陷。其余 harness 经攻击确认成熟，给 sign-off。

---

## 【低-中】1. failover 真正发生时，OTEL model 维度 + 缓存键报 primary，而非实际服务响应的 backup

**这是 R4 修复留下的"修对了 90%、暴露出最后 10%"。** R4 给两个 wrapper 加了 `.model` property
透传（`resilience.py:47-59` FailoverProvider、`resilience.py:98-104` CircuitBreakerProvider），
解决了我上轮报的"wrapper 无 `.model` → gateway 退回 tier 标签"。现在 plain / 容灾包装路径
解析出的都是真实模型名，单测 `test_gateway_records_real_model_through_resilience_wrappers`（`tests/test_resilience.py:219-230`）确认通过。**这一层确认修好。**

**新落差的根因（结构性，非表象）**：gateway 在调用**之前**解析 model 并冻结进 CallRecord + 缓存键：

- `gateway.py:231` `model = getattr(provider, "model", None) or tier` —— 对 wrapper 即 `fp.model`
- `gateway.py:232` `key = CacheKey(... model=model)` —— 缓存键此刻定型
- `gateway.py:250` 才真正 `provider.complete(...)`；**此处 FailoverProvider 才可能切到 secondary**
- `gateway.py:265` `CallRecord(model=model)` —— 仍是调用前那个 primary 值

而 `FailoverProvider.model`（`resilience.py:59`）写死返回 `getattr(self.primary, "model", "")`，
**永远是 primary**。`FailoverProvider.complete`（`resilience.py:61-68`）切到 secondary 后，
返回的 `(text, in, out)` 元组里**不带任何"我换模型了"的信号**——gateway 无从得知，
CallRecord.model / `gen_ai.request.model` / 缓存键全部停留在 primary。

**可复现（已实跑）**：
```
gateway resolves model (pre-call) = deepseek-v4-pro      ← primary
response actually produced by     = gpt-4o-mini          ← secondary 真正出的活
failovers counted                 = 1
=> CallRecord.model / gen_ai.request.model = 'deepseek-v4-pro'
=> 实际服务响应的模型               = gpt-4o-mini
=> MISMATCH: True
```
（primary 抛 503 availability error → FailoverProvider 切 secondary → secondary 出响应；
但 span/CallRecord/缓存键报 primary。）

**OTEL 语义约定层面（简报问的"符合吗"——诚实回答：部分不符，且漏了一个该有的属性）**：
- `gen_ai.request.model` 定义 = "the GenAI model **a request is being made to**"。failover 时
  请求**先发给 primary（失败）、再发给 secondary（成功）**，把它记成 primary 属于"记录了
  发起的那个、忽略了实际承接的那个"——语义上勉强可辩（毕竟先向 primary 发起），属灰区。
- 真正的硬缺口：约定里有专门的 `gen_ai.response.model` = "the model that **generated the response**"，
  这个在 failover 下应是 secondary。**全代码库 grep `gen_ai.response.model` 零命中**——该属性根本没设。
  即：观测者在 Jaeger/Tempo 上**完全看不出本次响应其实来自备用模型**，容灾发生这一事实在 trace 里隐身。
  对一个把"OTEL GenAI 语义约定对标"当卖点的 harness，这是最该补的一处。

**额外一层 correctness 影响（比可观测更值得在意）**：同一个 primary `model` 进了缓存键
（`gateway.py:232`）。failover 切 secondary 拿到的响应，会以 **primary 的 model 键**写回缓存
（`gateway.py:268-269`）。后续 primary 恢复、同 prompt 再来时，会**命中这条其实由 secondary 产出的缓存**——
即"备模型的答案被当成主模型的答案复用"。这正是 plain 路径缓存键设计要防的串答案问题
（`gateway.py:228-230` 注释明言"两模型共享 tier 必须靠 model 区分"），在 failover-发生瞬间被破。
触发窗口窄（要恰好 failover + 之后 primary 恢复 + 同 prompt），但语义上确实是污染。

**为什么没被测出来（与 R4 同一盲区，只是更深一层）**：R4 新增的 5 条 `.model` 测试
（`tests/test_resilience.py:178-252`）**全部用 primary 成功的场景**——
`test_gateway_records_real_model_through_resilience_wrappers` 的 primary 不抛错，所以记的就是 primary，
"对得上"纯属 primary 没让位。**没有一条断言"primary 失败 → failover 发生后 model 维度是否仍诚实"**。
failover-occurred × model-reporting 这个交叉格子，从 R3 到 R4 到现在一直是空的。

**是真 bug 还是设计/已兜底**：**真 bug，严重度【低-中】**。诚实定级理由：
- 严重度**低于**我上轮那条：上轮是"开容灾即退化"（条件=配置置位，命中面大）；
  这条要求**运行时真的发生 failover**（primary 实际故障），命中面窄、且只在故障期。
- 但它是**真实落差不是误报**：故障期正是你最依赖可观测的时候，偏偏此刻 trace 谎报模型 +
  `gen_ai.response.model` 缺失，且缓存键有窄窗污染。
- 不违反"诚实失败/不静默降级"红线的**功能**面（failover 本身是显式行为、`fp.failovers` 有计数），
  但违反**可观测诚实**面（外部观测者看不出换了模型）。
- 修复成本中等、非一行：FailoverProvider 需在 `complete` 里记录"本次实际命中 primary 还是 secondary"
  （如设 `last_used_model`），gateway 在 record 时优先取"实际用的模型"而非调用前解析值；
  并给 chat span 补 `gen_ai.response.model`。属于"把容灾这一等公民事件在可观测里如实呈现"的正解，
  不是打补丁。**求职展示角度：这条恰好能讲成一个有深度的 OTEL request/response model 区分点。**

---

## 末轮 sign-off — 我攻过并确认 OK 的点

以下经本轮针对性攻击，确认**无回归 / 已成熟 / 设计自洽**，不再列为问题：

- **R4 `.model` 透传本体修对**：plain / FailoverProvider / CircuitBreakerProvider / breaker-over-failover
  四种构造下，**primary 成功**时解析出的都是真实内层模型名（`resilience.py:47-59,98-104`，
  测试 `tests/test_resilience.py:178-252` 全绿）。内层无 `.model` 的离线 fake 正确返回 `""` →
  gateway 退回 tier，与 plain 离线一致（`test_wrapper_model_is_empty_when_inner_has_none`）。
  我上轮报的"开容灾即退回 tier 标签"**确认消除**。

- **CompressionCache 末轮总评（简报点名）= 对标业界、成熟**：
  失效语义（`context_compressor.py:140-165`）= 精确相等→零调用复用、严格前缀→只折新增、
  任何 head 改写/回滚/分支偏离→`return None` 全量重压。这与业界 prompt-prefix 缓存"按内容失效、
  偏离即作废"的语义一致；append-only 不变量即便被违反也只会**保守地多算一次**、绝不复用错摘要。
  缓存每 `run()` 内新建（`react.py:183`）不跨 run 泄漏。复用命中仍产出显式
  `[Summary of turns ...]` 标记（`context_compressor.py:281`）、stats 照常——**不静默降级**。
  唯一被缓存的是确定性输入的 summary 文本。**这一块是该 harness 里最扎实的工程，可放心当作品集亮点。**

- **tokenizer（tiktoken cl100k_base）成熟**：`tokenizer.py` 懒加载 + 缓存，缺包/加载失败/encode 失败
  三条退路都 `max(1, len//4)` 兜底且**带 WARNING 不静默**（`tokenizer.py:60-77,106-108`）。
  CJK 动机讲得清楚、对标 tiktoken 正确。`count_tokens("")==0`、非空恒≥1，无崩溃面。**确认 OK。**

- **OTEL 主体合规**：`gen_ai.operation.name` 三取值 `invoke_agent`/`chat`/`execute_tool` 均为约定合法枚举；
  span 树父子结构（`otel_bridge.py` 文件头 + 三个 context manager）与约定一致；
  SQLite exporter 的 `run_id==trace_id` 不变量自洽、`query_by_run_id` 可回放；
  no-op shim（`otel_bridge.py:524-566`）让 OTEL 关闭时调用点零分支、不抛。**主体确认成熟。**
  （遗留的两处低/记录级软偏差仍在：`gen_ai.system="owcopilot"` 填了产品名而非 provider
  `otel_bridge.py:370`；chat span 名硬编码 `"gen_ai.chat"` 而非约定推荐的 `"{op} {model}"`
  `otel_bridge.py:398`。R4 未动，**仍属记录级、非 bug**，与本轮 #1 的 `gen_ai.response.model` 缺失
  可一并作为"OTEL 对标补完"的小批次。）

- **react.py model 回填链路（plain + cache-hit）确认对**：`_update_chat_span_from_telemetry`
  （`react.py:499-517`）取 `records[-1].model`，cache-hit 也照记一条带真实 model 的 CallRecord
  （`gateway.py:237-247`）；`records[-1]` 不被 compact 记录错位（compact 在 step 内先于 planning）。
  **唯一不诚实的就是 #1 那个 failover-occurred 瞬间**，其余路径 model 维度诚实。

- **AgentStep.result 新字段（R4 引入）跨 provider 一致**：`react.py:274-317` 仅在
  `isinstance(result, dict)` 成功路径填 `structured_result`，错误/非 dict 路径为 `None`——
  与 provider 类型无关（result 来自 `registry.run` 工具输出，不经 LLM provider），
  不存在"某 provider 路径漏填"问题。**确认 OK。**

---

## 一句话总结（给用户）

末轮 sign-off：R4 的 `.model` 透传修复对、CompressionCache/tokenizer/OTEL 主体经攻击确认成熟可当作品集亮点；
唯一新落差是【低-中】"运行时真发生 failover 时 `gen_ai.request.model`+缓存键仍报 primary、且全程缺 `gen_ai.response.model`，
故障期 trace 看不出已切备用模型"（`resilience.py:59` + `gateway.py:231,265`，可复现，故障期窄窗还有缓存串答案的微风险）——
修它正好能讲成一个有深度的 OTEL request/response-model 区分点。
