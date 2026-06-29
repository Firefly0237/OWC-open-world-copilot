# R4 · 谨慎保守资深 agent 工程师视角

视角：agent 主循环 / 错误处理 / 并发·锁 / 资源管理 / 边界 / 状态传播 / 异常静默吞。
最在意「不静默降级」「真实落地」。HEAD=73d4d04，分支 feature/agent-pipeline-enhancements。

结论先行：**我领域没找到真 bug。** R3 在我领域的改动（CompressionCache 前缀缓存、
otel_bridge model/run_id 回填、gateway 未注册 tier 引导式错误）经针对性压测全部站得住。
缓存正确性尤其扎实——所有「transcript 非 append-only」的攻击向量都被正确处理。
下面是逐项验证记录 + 2 条「非 bug 但值得记一笔」的诚实标注。

---

## A. 重点攻击：CompressionCache 缓存正确性（brief 最关心）

缓存键 = `tuple(compactable_turns)`（内容寻址，checkpoint 已先过滤掉），
lookup 用「精确相等 or 严格前缀」判定。我逐个攻击 brief 点名的非 append-only 场景：

| 攻击场景 | 预期 | 实测结果 |
|---|---|---|
| **回滚/分支**（等长不同内容 [A,B,C]→[A,B,X]） | 不复用旧摘要 | ✅ 重算 S#2，未复用 S#1（PROBE 1） |
| **中途裁剪头部**（[A,B,C]→[B,C]） | 不复用 | ✅ None 路径，重算（PROBE 2） |
| **头部从尾缩短成旧缓存前缀**（[A,B,C]→[A,B]） | 不复用（cur 比 cached 短） | ✅ `_is_prefix` len 检查挡住，重算 S#2（PROBE 4） |
| **checkpoint 错位改变 compactable 成员**（B 变成 Error: 行→compactable 由 [A,B,C] 变 [A,C]） | 不复用 | ✅ 前缀不匹配→重算（PROBE 3） |
| **前缀中段变异**（[A,B,C]→[A,B',C,D]） | 全量重算，不走增量 | ✅ `whole[:3]!=cached`→None→full recompress（PROBE 5） |
| **合法增量**（[A,B,C]→[A,B,C,D]） | 只 fold D | ✅ 增量提示含 "Existing summary:"，只送 1 个新 turn（PROBE 6） |

**根因为何安全**：`lookup` 的 `cur == self._cached_turns`（精确）和 `_is_prefix(cached, cur)`
（`len(prefix)<len(whole) and whole[:len]==prefix`）都是**逐元素内容比较**，不靠
长度/位置。只要任一历史 turn 的字符串内容变了，相等与前缀检查同时失败 → 落到 `None` 全量重算。
因此「复用错摘要」在结构上不可能发生。**缓存键不会碰撞**（直接比 tuple 内容，无 hash）。

**并发共享安全**：`CompressionCache()` 在 `react.py:177` 的 `run()` 内部 new，**不挂 self**
（PROBE 9 验证 `self.compression_cache` 不存在）。所以同一 ReActAgent 实例的并发/串行多次 run
各自持有独立 cache，无跨 run 泄漏（PROBE 10）。brief 的「并发 run 共享 cache 安全吗」= **安全**。

判定：**真·无 bug**。缓存正确性是 R3 的亮点，不是隐患。

---

## B. 重点攻击：cache-hit 路径 model 名回填（brief 点名）

`react.py` 主循环顺序：`_compress_view`（可能触发 compact 调用，记一条 telemetry）→
`gateway.complete(task=agent_react)`（再记一条）→ `_update_chat_span_from_telemetry` 读
`records[-1]`。

- **PROBE 7**（真 client-cache 命中）：第二次相同 run 命中 client cache，provider 0 调用，
  但 gateway 仍记 `CallRecord(model=真实 model, cache_hit=True, in=0,out=0)`（gateway.py:237-247）。
  → 回填的 `gen_ai.request.model = "deepseek-v4-pro"`（真实 id），**不是** tier 标签或占位符。✅
- **PROBE 8**（compact 模型≠agent 模型）：compact 用 `compact-tiny-model`、agent 用
  `agent-big-model`。顺序 compact→agent_react 后，`records[-1]` 是 agent 记录 →
  chat span 拿到 `agent-big-model`，**没被 compact 模型污染**。✅

判定：**真·无 bug**。cache-hit 路径 model 名正确。

诚实补一点（非 bug）：cache-hit 时 `gen_ai.usage.input_tokens=0`。这是**诚实表示**
（client 命中 $0、确实没发 token 给 provider），与 telemetry 自身 `cache_hit=True` 语义一致，
不是漏报。

---

## C. 重点攻击：gateway 未注册 tier 引导式错误（brief 点名 R3 改动）

`gateway.py:210-227`：router 选了无 provider 的 tier 时，抛 `LLMGatewayError(category="config",
attempts=0)`，消息列出已注册 tier，引导「注册 provider 或改 router 映射」。

- PROBE 12：unregistered tier → 抛 LLMGatewayError，category=config，消息含已注册 tier 列表，
  attempts=0（从未触达 provider）。✅ 非静默、可操作。
- PROBE 13：空 providers dict → 消息显示 `(none)`。✅

判定：**真·无 bug**，符合「引导式错误，不抛裸报错」红线。

---

## D. 重点验证：上一轮我提的「压缩每步重算 O(steps)」是否真修好

brief 要求「验证修复真有效（compact 调用次数真降）且没引入缓存 bug」。

**关键纠偏（诚实）**：修复**没有降低 compact 调用*次数***——12 步前向 run，
有无 cache 都是 11 次调用（每步头部 +1 turn → 每步 1 次增量调用，PROBE 14）。
真正降的是**每次调用的输入工作量**（PROBE 15）：

| | 每次 compact 调用送进 LLM 的新 turn 块数 | 全程累计工作量 |
|---|---|---|
| 无 cache | 1,2,3,...,11（每步重算整个增长的头） | **66 = O(steps²)** |
| 有 cache | 1,1,1,...,1（只 fold 新 turn 到缓存摘要） | **11 = O(steps)** |

即修复把**累计 token 工作量从二次降到线性**。模块 docstring（context_compressor.py:111-133,
304-317）和测试（test_cache_incremental_only_compresses_new_turns 等）对此描述**准确诚实**
——它们说的是「incremental compaction / bounded input / O(steps)→~O(1) 额外调用」，
指的是「每轮额外工作量」而非「调用次数」。没有过度承诺。

判定：**修复真有效**（累计工作量 O(n²)→O(n)），**未引入缓存 bug**（A 节全过）。

---

## E. 非 bug 但记一笔（诚实标注，不夸严重度）

### E1.【信息/极低】summary marker 的「turns 1-N」标号是计数而非原始下标
- 证据：context_compressor.py:279-281，`first_idx=1; last_idx=n_compacted`。
  当 head 里夹着 checkpoint（如中段 `Error:` 行），compactable=[A,C,D] 时 marker 仍写
  `turns 1-3`，而 A/C/D 在 transcript 的真实位置是 0/2/3（PROBE 16）。
- 影响：纯 cosmetic。checkpoint 被提到 view 最前 + 摘要 + tail，**所有内容模型都看得到，零数据丢失**。
  模型不依赖「1-3」这串数字做任何事。
- 判定：**非 bug**（设计上的近似标号），不建议改——改了要传原始 index、收益为零。

### E2.【设计权衡/非 bug】增量 fold 是「摘要的摘要」，长 run 可能信息衰减
- 证据：_run_compaction 增量分支（context_compressor.py:321-327）把上轮**摘要**再喂回去和新 turn
  一起重摘。多步后是 summary-of-summary，理论上比「每步全量新鲜摘要」更易丢早期细节。
- 影响：这是增量压缩的**固有质量↔成本权衡**，docstring 已点明是 incremental。且 LLM 输出本就非确定，
  谈不上「正确性 bug」。如果哪天质量比成本更重要，可加「每 K 轮强制全量重压」策略——但当前
  product north star 是稳定/省钱，增量是合理默认。
- 判定：**非 bug**，已知 v1 增强方向，不属于「静默降级」（每轮仍有显式 `[Summary ...]` marker）。

---

## F. 验证 OK 清单（我领域，全绿）
- 缓存正确性：回滚/分支/裁剪/checkpoint 错位/中段变异/合法增量 6 类攻击全部正确（PROBE 1-6）。
- 并发：CompressionCache per-run 隔离、不挂 self（PROBE 9-10）。
- cache-hit model 回填正确、compact 模型不污染 chat span（PROBE 7-8）。
- 未注册 tier / 空 providers → 引导式错误、attempts=0、非静默（PROBE 12-13）。
- O(steps²)→O(steps) 修复真有效且无缓存 bug（PROBE 14-15）。
- gateway 失败 → graceful 回退未压缩 view、triggered=False、不静默降级（既有测试 + 复核代码 270-274）。
- telemetry 无锁的 snap/since 窗口：**实践安全**——service/api.py 每个请求 handler 各建独立
  `TelemetryCollector`（api.py:1420）+ 独立 gateway（1427），jobs.py 每 job 独立 worker 线程，
  无共享 telemetry 的并发 agent run。故「records_since 窗口在并发下错配」不可达，非真 bug。
- 主循环异常处理：SkillError / 通用 Exception 均转 Observation 不崩（react.py:271-292），
  span 标 ERROR + record_exception，no-op span 下 try/except 兜住不二次抛。
- 测试：test_t4_context_compressor + test_agent_react + test_t4_otel_bridge + test_agent_step_span
  + test_phase_b_implementations = **134 passed**。

---

一句话：我这轮在 agent 主循环/错误处理/并发/资源域**没找到真 bug**；R3 的 CompressionCache
缓存正确性经 6 类「非 append-only」攻击全部站得住、cache-hit 的 model 回填正确、未注册 tier 已是
引导式错误，且上一轮我提的「压缩每步重算 O(steps²)」确实被修成线性（注意修的是每轮工作量而非调用
次数，docstring 描述诚实）；只留 2 条 cosmetic/设计权衡级的诚实标注，均非 bug、非静默降级。
