# R3 · multi-agent / sub-agent 系统深审

身份：专注 multi-agent / orchestrator-worker / 黑板 / verifier 的技术极客
被测：`F:\openworld\src\owcopilot\multi_agent\*`，HEAD = `8cfd19b`（R2 fixes）
gates：`pytest tests/test_multi_agent.py` 31 passed；mypy 7 files clean；ruff clean。

---

## 先确认 R2 的 RT3 大修是否真修了（结论：真修了）

逐项核对，全部坐实，非搭壳：

- **① worker 崩溃记 failed task_result**：`session.py:209-240` try/except 包 `run_task`，崩溃→`update_status(claimed.id,"failed")`+写 stop_reason="error" 的 task_result。`test_rt3_1` 真复现（monkeypatch 抛 RuntimeError）并断言 blackboard 有 status="failed" 的 result。✅
- **② LLM 分解失败标 decomposition_degraded**：贯穿 `post_task_assignments`→`synthesize`→`MultiAgentReport.decomposition_degraded` 字段+synthesis 文本 `[WARNING] 分解已降级到静态模板`。`test_rt3_2` 断言。CLI 也透出（`main.py:783`）。✅
- **③ 未注册 worker_id 记 unrouted**：`session.py:151-182` worker is None→warning+写 stop_reason="unrouted" 的 failed task_result+原 assign 标 failed。`test_rt3_3` 真复现。✅ 我实跑确认。
- **④ repair worker 真读 diag 结果**：`_enrich_repair_task` 把 diag.final_answer 嵌进 repair subtask，原 generic assign 标 done（superseded），enriched assign 重新 post 供 claim。**我实跑 full session 验证**：flow 里出现两条 `repair_01` assign，第一条 `subtask_has_diag=False [done]`，第二条 `subtask_has_diag=True` 被 claim。真 diag→repair handoff，无竞态（原条 done 不可 claim，claim_task 按 created_at ASC 只能拿到 pending 的 enriched 条）。✅
- **⑤ 死断言 `or True` 改真断言**：`test_rt3_5` 用 `first_assign.created_at <= first_result.created_at` 真比较时序。✅
- **⑥ verifier needs_more 标注**：`_build_synthesis` 把 needs_more 渲染成 `needs_more [INCOMPLETE — verifier could not finish audit]`，与 pass/fail 区分。✅
- **⑦ orchestrator 自身 ReActAgent 用上**：`_decompose_goal` pass-2 调 `self.agent.run(meta_goal)`。**实例真存在、真独立**（`test_rt3_7` 断言 `orch.agent is not 各 worker.agent`）。但"用上"的质量见下方【中-3】。

**真 multi-agent 群的核心证据点（确认 OK）：**
1. 4 个独立 class（Orchestrator/Diag/Repair/Verifier），各自 agent_id、各自 ReActAgent 实例、各自 transcript（`test_p3_1_*`、`test_p3_7_worker_transcripts_are_not_same_object` 都真断言 `is not`）。
2. 通信全走 SQLite 黑板（`agent_messages` 表），非函数返回值串调用栈。`claim_task` 用条件 UPDATE（`WHERE status='pending'`）做乐观锁，`test_p3_3_claim_prevents_double_claim` 真验二次 claim 返回 None。
3. **verifier 真独立**：`verify()` 签名只吃 `verify_req`+`blackboard`，全程不引用任何 worker 对象，只 `blackboard.get_message(target_msg_id)` 读 payload。grep 确认 verifier.py 里唯一提 `worker.agent._transcript` 的是"NEVER reads"的注释。架构级隔离，非 prompt 约束。✅
4. 共享 gateway 实例不构成 echo-chamber：gateway 每次 `complete()` 无状态（无跨调用对话历史），prompt 按 agent 不同，cache key 含 system+user 不会串味。P3-5"不共享 model instance"在精神上满足。

> 前两轮确实清掉了一大批真缺陷。剩余问题集中在**"可运行路径下 verification 是不是真在 verify"**这一层。

---

## 残余真问题

### 【高】verifier 在唯一可运行（离线）路径下永远 needs_more / verified=0 —— "独立 ground-truth 验证"实际不产出验证

**根因（多重叠加，非单点）：**
1. `VerifierAgent` 用 `max_steps=3`（verifier.py:54）。离线唯一的 `OfflineReactProvider`（`agent/offline.py:107`）跑**固定 4 步脚本** `audit_project→build_context_pack→quality_harness→Final Answer`，与 goal 无关。3 步永远到不了第 4 步的 Final Answer → `stop_reason="max_steps"` → `_compute_verdict` 第一条就 return `needs_more`（verifier.py:203）。
2. 即便步数够，`_extract_error_count`（verifier.py:163）只匹配**英文** `r"(\d+)\s+error"` 等；而离线 provider 的 Final Answer 是**中文**"一致性审计发现 2 个待修复错误" → 正则全 miss → fallback `answer.lower().count("error")` 在中文串上 = **0**。

**证据（实跑 CLI `multi-agent --llm-mode offline`，世界含 1 个真 dangling-ref `giver_npc='npc_ghost'`）：**
```
VERIFIER VERDICTS: 两条都是
  verdict="needs_more", open_errors_verified=0,
  rationale="Verifier reached max_steps before completing independent audit"
```
但底层 `audit_project` 真跑出来了 `"open": 2`（我单独跑 verifier 配置的 ReActAgent 确认 step0 observation = `{"open": 2}`，step2 quality_harness = `audit_totals.error: 2`）。**ground truth 触手可及，但 verifier 的"读数"层把它全丢了。**

**这是真 bug 还是已兜底？** 介于之间，但偏真问题：
- needs_more **被显式标注**（`[INCOMPLETE]`），所以**不算静默降级**——红线 #2 守住了。
- 但红线 #1（真实落地 / agent 真能完成任务）**没守住**：唯一能跑的路径里，verifier 从不交付一个真实 pass/fail 判定，也从不真把 worker 的 open_errors 跟自己的独立审计对比。"独立 ground-truth 验证"是整个 P3-5 的卖点，离线却**结构性地无法产出**。求职展示时若被追问"给我看一次 verifier 真的 catch 了 worker 虚报"，离线没有这个 demo。

**修法方向（不打补丁）：** verifier 的 `max_steps` 至少给到能走完离线脚本（≥4），并加真正的**确定性兜底**——当 ReAct 没给出可解析计数时，verifier 直接调 `registry.run("audit_project")`/`list_issues` 拿 `open` 字段（这正是 verifier.py:24-25 文档**声称存在但代码里没有**的 `_deterministic_verify` 路径，见下条）。计数解析要同时认中文（"N 个…错误"）或干脆解析工具 JSON 的 `open_errors`/`open` 字段而非自然语言。

---

### 【中】文档声称的 `_deterministic_verify` / "fall back to deterministic" 兜底**根本不存在**（phantom 路径）

**根因：** verifier.py 文档与注释三处承诺一个确定性兜底，代码里查无此方法：
- L24-25：`"falls back to counting open issues from the store directly via the _deterministic_verify path"`
- L77：`"Run our own independent ReActAgent (or deterministic fallback) to audit"`
- L130：`"try LLM answer first, fall back to deterministic"`

实际只有 `_extract_error_count`（对同一个 answer 串再 `count("error")`，**不是**读 store/工具的确定性兜底）。`grep "_deterministic"` 在整个 `multi_agent/` 只命中这两行**注释**，无定义。

**证据：** `grep def _deterministic verifier.py` → 0 命中（只有 docstring）。

**真 bug 还是设计选择？** 真问题——**文档撒谎/代码缺失**。它让读者（含面试官 review 代码时）以为有一条 store-level 确定性兜底保证 verifier 永远有真数，实际没有。这正是上一条【高】的直接成因之一。属"搭壳"信号：声称的能力没实现。建议：要么补上真兜底（推荐，顺手解掉【高】），要么删掉这些虚假文档。

---

### 【中】orchestrator pass-2 meta-ReActAgent 结构上不可能成功 —— ⑦的"激活"偏表演

**根因：** `_decompose_goal` pass-1 直连 gateway 要 JSON 失败后，pass-2 调 `self.agent.run(meta_goal)`。但 `self.agent` 配的是 `allowed_skills={"audit_project","list_issues"}`（审计工具，**与分解任务无关**）、`max_steps=2`，跑在**同一个**刚失败的 gateway 上。离线 provider 无视 goal、跑审计轨迹，max_steps=2 永远到不了 Final Answer → 返回"Reached step budget"消息（**零 JSON**）→ `_parse_subtasks` 必然 `degraded=True` → 落静态模板。

**证据（实跑）：**
```
orch._decompose_goal('make exportable') → degraded=True
orch.agent.run(...).final_answer = "Reached the step budget before finishing..."
orch.agent.run(...).stop_reason = "max_steps"
```
即 pass-2 **每次必败**，对 outcome 零贡献。

**真 bug 还是设计/已兜底？** 偏"弱设计 / 半表演"，不到真 bug：
- 它**诚实降级**（最终 degraded=True 被透出），不违红线 #2。
- 真 LLM 下理论上**可能**有用（若 frontier 返回了 verbose 但可抽取的 JSON），所以不是纯死代码。
- 但把一个**只装了审计工具**的 ReActAgent 派去做"从 verbose 文本抽 JSON 子任务"，工具面与任务不匹配，`max_steps=2` 也太短。这是为满足 rubric"orchestrator 要有自己的 agent 且用上"而做的**最小化激活**，工程上偏薄。建议：pass-2 要么直接二次 `gateway.complete`（换 system 提示"只输出 JSON"）而非走审计-ReAct，要么诚实标注此 agent 仅作 meta 占位。不算严重，但"是不是真用上"经不起深问。

---

### 【低】`TaskAssignPayload.max_steps` 是死协议字段 —— worker 永不读取

**根因：** `TaskAssignPayload` 带 `max_steps`（messages.py:88），`_enrich_repair_task` 也认真保留它（session.py:314）。但 `WorkerAgent.run_task`（workers.py:74-105）**从不读 `assign.max_steps`**，永远用构造期的 `self._max_steps`（默认 4）。orchestrator/分解 JSON 给的 per-subtask 步数预算被静默忽略。

**真 bug 还是？** 真但低——是个"协议宣传了控制旋钮，实现没接线"的搭壳小点，不影响正确性（只是预算不可调）。建议 `run_task` 用 `assign.max_steps` 覆盖默认，或从 payload 删该字段。

---

### 【低】`allowed_skills` 只过滤 manifest，不在执行层强制 —— "minimal attack surface" 名不副实

**根因：** `SkillRegistry.run(name, args)`（`core/skills/__init__.py:131-133`）按名 dispatch，**完全不检查 `allowed`**。`allowed_skills` 仅作用于 `manifest(allowed=...)`（只改 prompt 里展示的工具清单）。

**证据（实跑 verifier 配置 `allowed={audit_project,list_issues}`）：** 离线 provider 第 1、2 步调 `build_context_pack`、`quality_harness`（**都不在** allowed 集），两步均 `is_error=False` 成功执行。即白名单是 cosmetic。

**真 bug 还是？** 真但低、且**非 multi_agent 独有**（是共享 ReActAgent/registry 的既有性质）。但它戳穿了 `messages.py:87` 注释"tool whitelist for minimal attack surface"——离线下 verifier 实际就越界调了两个非白名单工具。若把"最小工具面/隔离"作为 multi-agent 安全卖点展示，这条会被抓。建议 `ReActAgent` 在 `registry.run` 前对 `parsed.action not in allowed_skills` 拒绝并回 error observation。

---

### 【低】终态消息永久 `pending` + 死字段 `confidence`

- worker 的 `task_result`、verifier 的 `verify_result`、orchestrator 的 `synthesize` **从不被 `update_status` 标 done**（只有 assign 被标）。实跑 flow 里这些行永远 `[pending]`。无功能影响（它们是终态记录不被 claim），但 status 列误导。
- `TaskResultPayload.confidence`（worker 写 0.8/0.4，session 写 0.0）**全项目无任何读取处**（grep 确认）。死字段。

均属状态卫生 / 死代码，低优先。

---

## 一句话总结

R2 的 RT3 七项大修全部坐实非搭壳、verifier 架构级独立也是真的（这是真 multi-agent 群）；**但唯一可运行的离线路径下 verifier 因 max_steps=3+中文计数失配+文档承诺却不存在的 `_deterministic_verify` 兜底，永远只吐 needs_more/verified=0，从不真正完成"独立 ground-truth 验证"——needs_more 虽被显式标注（未违静默降级红线），却踩了"真实落地"红线，是本轮最值得修的一条【高】。**
