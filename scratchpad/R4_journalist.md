# R4 — Harness 成熟度复测（资深 agent-harness 记者视角）

被测：OWCopilot，分支 `feature/agent-pipeline-enhancements`，HEAD=`73d4d04`（R3 fixes）。
聚焦 harness：上下文压缩 / OTEL 可观测 / token 预算·tokenizer / 记忆·lesson / tool 注册调用。

任务双线：(1) 找我领域残余真问题；(2) **压测 R3 新代码找回归**——我上轮报的 3 条
（中：model 误填 task / 低：critic-lesson general 措辞 / 低：run_id 悬空）刚被修，重点验证修对没修全。

结论先行：**R3 三条修复方向都对，但其中【中】model 回填只修了"主路径"、漏了"resilience 包装路径"——
在生产可观测里是一个真实的、可复现的回归级残留缺陷（中）**。另确认 2 个 OTEL 语义约定的软偏差（低/记录级），
其余 R3 修复 + 压缩缓存设计经攻击后**确认成熟**。

---

## 【中】1. R3 的 model 回填修复对 resilience 包装 provider 失效 —— `gen_ai.request.model` 在生产容灾路径退回 tier 标签

**这是我 R3【中】号缺陷的"修了一半"。** R3 的修法是：CallRecord 加 `model` 字段，gateway 用
`model = getattr(provider, "model", None) or tier`（`gateway.py:231`）解析真实模型名，react.py 从
`telemetry.records[-1].model` 回填 span（`react.py:499-500`）。对 **plain `OpenAICompatProvider`** 完全正确。

**根因（非表象）**：真实生产 provider 由 `build_real_provider()` 构建（`resilience.py:117`），它在
studio 开启容灾时把 `OpenAICompatProvider` 包进 `FailoverProvider` / `CircuitBreakerProvider`：

- `src/owcopilot/llm/resilience.py:148` `provider = FailoverProvider(provider, secondary)`
- `src/owcopilot/llm/resilience.py:150` `provider = CircuitBreakerProvider(provider, ...)`

而这两个 wrapper **都没有 `.model` 属性**（`FailoverProvider.__init__` 只存 `primary/secondary`，
`CircuitBreakerProvider.__init__` 只存 `inner`——`resilience.py:42-45, 75-82`）。于是 gateway 的
`getattr(wrapper, "model", None)` 返回 `None` → 回退 `or tier` → `model="cheap"`。

**这正是 R3 要消灭的症状本身**（"gen_ai.request.model 携带内部标签而非真实模型名"），只是搬到了另一条路径。

**可复现**（已实跑 `scratchpad/repro_resilience_model.py`）：
```
plain provider .model     : deepseek-v4-pro
FailoverProvider .model   : None
CircuitBreaker .model     : None
=> gateway: model = getattr(provider,'model',None) or tier
   plain    : gen_ai.request.model -> 'deepseek-v4-pro'
   failover : gen_ai.request.model -> 'cheap'      ← 回归
   breaker  : gen_ai.request.model -> 'cheap'      ← 回归
```

**触发条件 = 文档化的生产配置**：`OWCOPILOT_FALLBACK_MODEL` 或 `OWCOPILOT_CIRCUIT_BREAKER=1`
任一置位（`resilience.py:128-133` 头注里就是教 studio 这么开容灾）。这三个真实落地入口全走
`build_real_provider`：`cli/main.py:514`、`app/actions.py:1033`、`service/api.py:1423,1492`。
即：**任何开了容灾的真实部署，OTEL "model" 维度直接退化成 tier**，Jaeger/Tempo/Grafana 的模型聚合、
成本归因看板按 `"cheap"` 分组——和 R3 修复前同样失真。

**额外一层 correctness 影响（不止可观测）**：同一个 `model` 变量还被用作 **缓存键的一部分**
（`gateway.py:231-232` → `CacheKey(... model=model)`）。容灾从 primary 失败切到 **secondary**（不同真实模型，
`resilience.py:54`）时，两个模型的请求都以 `model="cheap"` 入键 → **缓存键无法区分主/备模型**，
理论上可把备模型的答案命中回主模型。这比纯可观测失真更值得修。（注：plain 路径 R3 注释 `gateway.py:228-230`
特意强调"两模型共享 tier 必须靠 model 区分"——wrapper 路径恰好破了这个不变量。）

**为什么没被测出来**：`tests/test_resilience.py` 全程用自带 `.model` 的 fake provider，**无一条断言
wrapper 自身暴露 `.model`**，也没有"开容灾后 gen_ai.request.model 仍为真实模型名"的端到端断言。
R3 的 model 单测仍是直接喂 `gen_ai_chat_span(model=...)`，绕过 wrapper。所以这条回归落在测试盲区。

**是真 bug 还是已兜底**：**真 bug（回归级）**，严重度「中」：
- 默认 real-mode（不开容灾）不受影响，plain 路径 R3 修复成立；OTEL 默认关闭。
- 但容灾是文档主推的生产配置，一旦开启即退化，且缓存键退化有潜在串答案风险。
- 修复极低成本：给两个 wrapper 加 `@property model` 透传内层（`return getattr(self.inner/self.primary, "model", "")`），
  或 gateway 改用递归 unwrap 取 model。一行 property 即可让 R3 修复覆盖全路径。

---

## 【低/记录】2. OTEL 语义约定的两处软偏差（非 R3 引入，对标级，非功能 bug）

复测时顺手对标了一遍 OTEL GenAI Semantic Conventions（v1.39+），两处与规范"推荐写法"有出入——
都不影响 agent 主流程，且属开放枚举允许的自定义，**记录不当真 bug 报**：

- **`gen_ai.system` 填 `"owcopilot"`（app 名）而非 provider**（`otel_bridge.py:370`）。规范里
  `gen_ai.system` 语义是底层 GenAI 系统/provider（`anthropic`/`openai`/`deepseek`…）。这里填了产品名，
  provider 维度信息丢失。属开放枚举可接受，但与约定本意有偏。
- **chat span 名硬编码 `"gen_ai.chat"`**（`otel_bridge.py:398`）。规范推荐 span 名为
  `"{gen_ai.operation.name} {gen_ai.request.model}"`（如 `"chat deepseek-v4-pro"`）。当前是静态串。
  纯命名美观，不影响属性聚合。

对标正面：`gen_ai.operation.name` 三个取值 `invoke_agent`/`chat`/`execute_tool` **都是规范枚举合法值**，
span 树父子结构（invoke_agent → chat → execute_tool）与约定一致——主体合规，上面两条是边角。

---

## 压测确认 OK 的点（R3 新代码经攻击后无回归）

- **R3 修复 #2（critic-lesson general 措辞）确认修好**：复查 `assist/lessons.py` 的替换逻辑，
  general 模板与 dimension 模板现已统一改写口径，我 R3 报的"生成时请整体提高"泄漏不再复现。
- **R3 修复 #3（agent.run_id 悬空）确认修好且修得干净**：`react.py:184-189` 现用
  `run_id = trace_id_of_span(root_span)` **无条件**回填（不再依赖永不传入的入参），SQLite exporter
  `run_id==trace_id` 不变量保持一致，文档承诺的 `agent.run_id` 属性现真实落在生产 span 上。
  `trace_id_of_span`（`otel_bridge.py:491-503`）对 no-op span 安全返回 ""，OTEL 关闭时不抛。
- **cache-hit 路径的 model 回填（plain）对**：gateway 在 client-cache 命中时仍 record 一条
  `CallRecord(input=0,out=0,cache_hit=True,model=model)`（`gateway.py:237-247`），react.py
  `_update_chat_span_from_telemetry` 取 `records[-1]`，`last.model` 为真实模型名 → span 拿到正确 model，
  token 回填 0/0（无 provider 调用，诚实）。**plain 路径 cache-hit 回填确认对**（resilience 路径同样受 #1 影响）。
- **`records[-1]` 不错位**：压缩(compact 记录)在 step 内先于 planning(agent_react 记录)发生
  （`react.py:192` 压缩 → `react.py:212` planning → `react.py:219` 回填），`[-1]` 始终是本次 planning。确认无交错。
- **CompressionCache 经"非 append-only/分支/回滚"攻击后稳健**：`lookup`（`context_compressor.py:150-155`）
  逐元素比对 `_cached_turns`——精确相等→复用；**严格前缀**（`_is_prefix` 要求 `len(prefix)<len(whole)`
  且逐位相等，`context_compressor.py:163-165`）→只折新增；**任何 head 改写/回滚/分支导致偏离**→落
  `return None, list(cur)` 全量重压。**即便违反 append-only 不变量也不会复用错摘要**——检测到偏离即重算。
  缓存每次 `run()` 内新建（`react.py:177`），不跨 run 泄漏。**对标业界（prompt-prefix 缓存按内容哈希失效）
  的失效语义到位，确认成熟**。gateway 失败仍优雅回退 `triggered=False`（`context_compressor.py:270-274`）。
- **缓存命中不静默降级**：exact-hit 复用也照样产出显式 `[Summary of turns 1-N (compressed): ...]`
  标记（`context_compressor.py:281`），stats 照常；唯一被缓存的是确定性输入的 summary 文本，行为/统计诚实。

---

## 一句话对标结论

R3 三条修复方向全对、压缩前缀缓存（CompressionCache）的失效语义对标业界且经攻击确认成熟；但 R3 的【中】
model 回填只覆盖了 plain provider，**漏了 `FailoverProvider`/`CircuitBreakerProvider` 容灾包装路径——
开启容灾的生产部署里 `gen_ai.request.model` 会退回 tier 标签（且同一退化波及缓存键，潜在串答案）**，
这是一个可复现的回归级残留缺陷（中，一行 property 即可补全），其余皆确认无回归。
