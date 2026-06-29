# R3 — 资深 agent 工程师视角复审（残余问题）

身份：谨慎/保守/边界与正确性优先。关注 agent 主循环、异常路径、并发/锁、资源管理、超时/重试、静默吞错。
HEAD = Round 2 fixes。下面区分「真 bug」vs「设计选择/已兜底」，并按严重度排序，不夸大。

---

## ✅ 先确认 R2 修复确实在位（验收 OK）

- **otel get_tracer 双检锁**：`llm/otel_bridge.py:209,231-233` 模块级 `threading.Lock` + 锁内 re-check，fast-path 无锁。正确的 check-lock-check。✔
- **execute_tool 异常 span = ERROR**：`agent/react.py:249-255`（SkillError）和 `260-266`（任意异常）都 `set_status(ERROR)` + `record_exception`，且包在 `try/except: pass` 里防 no-op span 反爆。✔
- **SQLite 绝对路径**：`otel_bridge.py:78-88` `_default_sqlite_path()` 落到 `~/.owcopilot/traces/`，非 CWD 相对。✔
- **multi_agent worker 崩溃记 failed**：`multi_agent/session.py:209-240` worker.run_task 包 try/except，崩溃时 `update_status(claimed.id,"failed")` + 写占位 task_result（stop_reason="error"），不再是幻象成功。✔
- 跑 `tests/test_multi_agent.py` → 31 passed。

---

## 真问题（按严重度）

### 【中-低】① 上下文压缩每步重算 —— O(steps) 次冗余 compact LLM 调用
**根因（非表象）**：压缩是 read-time、append-only 设计（`react.py:170-182` 每步调 `_compress_view`）。
一旦 transcript 超过 `budget*threshold`，**之后每一步都会重新发起一次 `task="compact"` 的 gateway.complete**，
每次都把不断增长的 head 从头摘要一遍——没有缓存上一轮 summary、也没有增量扩展。摘要结果正确、统计诚实
（非静默降级），但代价是 token+延迟的线性放大。

**证据（可复现）**：用一个让 transcript 持续增长的 provider 跑 6 步、budget=300：
```
steps: 6  stop: max_steps
context_compressions(reported): 3
compact LLM calls actually made: 3      # 第 4/5/6 步各触发一次，重复摘要重叠内容
tasks: [agent_react, agent_react, agent_react, compact, agent_react, compact, agent_react, compact, agent_react]
```
**是否真实可达**：是。CLI `agent` 命令（`cli/main.py:711-713`）构造 ReActAgent **不传 token budget**，
落到默认 `transcript_char_budget=20_000 → token_budget=5000`，阈值 3500 tokens。真实 `--llm-mode real` 长跑
（多步、大 observation）必然触发，每步多花一次 frontier/cheap compact 调用。
**定性**：真 bug 类「效率/成本放大」，**不是**静默降级红线（输出与统计都诚实）。
成熟做法：把已生成的 `summary_marker` 缓存进 transcript 视图、后续步仅对「上次摘要之后的新 head 增量」再压，
或对同一 head 文本做 compact 结果 memo。属「不打补丁、参考成熟方案」范畴的改进点。

### 【低】② LLMGateway 对未注册 tier 抛裸 `KeyError`（违反「guided errors, not raw」，但生产不可达）
**根因**：`llm/gateway.py:209 provider = self.providers[tier]`。若 router 选出的 tier 不在 `providers` dict，
直接裸 `KeyError`，无友好引导。
**证据（可复现）**：
```python
gw = LLMGateway(providers={"cheap": MockProvider()})   # 默认 StaticRouter
gw.complete(task="generate", ...)   # DEFAULT_MAP 把 generate->"frontier"
# -> KeyError('frontier')   裸报错
```
**是否真实可达**：**生产路径不可达**。所有生产 builder（`cli/main.py:518-523`、`app/actions.py:1041-1044`、
`service/api.py:1429`）都显式传 `StaticRouter(mapping={全部任务:"cheap"})` 且 `default_tier="cheap"`，
只会路由到已注册的 `"cheap"`。所以这是**靠约定守住的潜在健壮性缺口**，不是当前 live bug。
**定性**：真 bug 类「裸错误未引导」，但严重度低（约定兜底）。一行防御即可：tier 不存在时抛
`LLMGatewayError`/带可用 tier 列表的友好消息，符合「guided errors」记忆红线。

### 【低】③ `multi_agent/__init__.py` docstring 示例直接报错（文档 bug）
**根因**：README/模块 docstring 示例 `LLMGateway({"default": MockProvider()})`（`multi_agent/__init__.py:17`）。
默认 StaticRouter 永远路由到 `"cheap"`（或 generate/repair 的 `"frontier"`），都不是 `"default"`。
**证据**：照抄示例 `gw.complete(task="orchestrator_decompose",...)` → `KeyError 'cheap'`。
**定性**：纯文档/示例 bug（不执行，不影响产品），但作品集对外可见、会误导读者。建议改成
`LLMGateway({"cheap": MockProvider()})`。与 ② 同源（providers key 必须覆盖 router 可能返回的 tier）。

### 【低】④ `MultiAgentSession.close()` 文档与行为不符（注入 conn 被误关）
**根因**：`session.py:271-276` `close()` 注释写「no-op if conn was injected externally」，
但 `__init__`（64-85）**没有记录 conn 所有权**，`close()` 无条件 `self._conn.close()`。
若调用方注入了与 SQLiteStore 共享的 conn，session.close() 会把人家的连接也关掉。
**是否真实可达**：当前**不可达**——唯一生产调用方 `cli/main.py:753` 不传 conn（自建 `:memory:`），自己关自己正确。
注入共享 conn 的路径目前无人用。
**定性**：潜在 bug + 文档说谎。修法：`__init__` 存 `self._owns_conn = conn is None`，`close()` 仅在拥有时关。

---

## 验证过、确认 OK 的点（无问题）

- **gateway 重试/失败闭合**（`gateway.py:252-273`）：`_complete_with_retries` 把最终异常包成
  `LLMGatewayError`（带 task/tier/category/attempts），从不静默降级。✔
- **resilience 熔断器锁**（`resilience.py:79-110`）：`is_open`、`complete` 的失败计数/开闸/半开
  全程持锁；失败先 `raise` 再在锁内累计，不吞异常。FailoverProvider auth 透传不掩盖配置错。✔
- **JobManager**（`service/jobs.py`）：单 `Condition` 守护 jobs/events，worker 线程异常→job.status=failed
  并 emit，不静默；`_prune_locked` 在锁内裁剪。SSE wait 有 deadline，不会无限阻塞。✔
- **ExactCache / SemanticCache / RedisCache**（`llm/cache.py`）：各自 `threading.Lock`；SemanticCache
  锁内快照、锁外扫描（避免 list 变更迭代崩）；RedisCache 懒建客户端双检锁。✔
- **otel SqliteSpanExporter**（`otel_bridge.py:154-190`）：单连接 `check_same_thread=False`，但写入只来自
  `BatchSpanProcessor` 单后台线程，无并发写；DB 写失败返回 FAILURE + warning（不静默）。✔
- **tokenizer fallback**（`llm/tokenizer.py`）：tiktoken 缺失/损坏→`len//4` 但带 UserWarning，
  非静默降级；`count_tokens` 所有异常兜回 fallback，永不崩。`_ENCODER` 全局无锁但赋值幂等，竞态无害。✔
- **react 主循环异常路径**：tool 崩溃（`react.py:256-266`）记 is_error=True 当 observation 喂回，不 crash；
  observation 超限按 `_OBSERVATION_CHAR_LIMIT` 截断且带显式 `[truncated N chars]` 标记（非静默）。
  parse 失败/无 Action 时 nudge 一步而非崩。✔
- **blackboard claim 乐观锁**（`blackboard.py:98-137`）：条件 UPDATE `WHERE status='pending'`，
  rowcount==0 视为被抢，无 TOCTOU。`update_status` 白名单校验状态值。✔
- **verifier 缺失 target_msg**（`verifier.py:88-105`）：找不到目标时显式发 verdict="fail"，不静默。✔
- **decomposition 降级**（`orchestrator.py:117-126,420-425`）：LLM 分解失败→静态模板 + `degraded=True`
  一路透传到 synthesis 文本/report 字段/CLI 输出，warning 大写标注，完全符合「不静默降级」。✔

---

## 一句话结论
没找到严重/高级真 bug；R2 那批并发/异常修复都在位且正确。残余只有 4 条中-低：最值得做的是
①「压缩每步重算 → O(steps) 次冗余 compact 调用」（真实可达的成本放大，非静默降级），其余 3 条
（gateway 裸 KeyError、broken docstring 示例、close() 误关注入 conn）都靠约定/当前无人走而暂不可达，属潜在健壮性/文档债。
