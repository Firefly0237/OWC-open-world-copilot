# R5 (末轮) — multi-agent/sub-agent 深审 + R4 回归压测

身份：专注 multi-agent/sub-agent 系统的技术极客。红线=真 multi-agent 非搭壳 + 不静默降级 + 真实落地。
范围：`src/owcopilot/multi_agent/*` + `agent/react.py`（AgentStep.result）。
方法：读全部 8 个模块 + 测试套件；写**独立**端到端复现（不复用项目 test helper），全部跑在**真实 product 路径**（真 content root + 真 `audit_project` + 真 `default_skill_registry` + 完整 `session.run`）。

---

## 找到的真 bug（1 条，已修）

### 【LOW（真 bug，已修）】verifier 确定性路径对「非 dict 的 audit 返回」会崩溃，且不被 session 兜底
- **根因**：`verifier.py::_deterministic_verify` 第 220 行 `result = self._registry.run("audit_project", {})` 包在 try/except（catch `SkillError` + 兜底 `Exception`）里；但**第 227 行 `result.get("open_errors")` 在 try/except 之外**。若 `audit_project` handler 返回非 dict（list / None / str / int），`.get` 抛 `AttributeError`——这不是 `SkillError`、又在 try 块外——直接冒泡出 `verify()`。
- **放大**：`session.py::run` 第 248 行 `self.verifier.verify(...)` **没有** try/except 包裹（与 worker 第 208 行的 `worker.run_task` 包裹**不对称**），所以 verifier 这一崩会把**整个 `session.run` 打挂**。
- **对称性证据（这是关键，说明是遗漏不是设计）**：worker 侧读同一个 audit 结果的 `workers.py::_extract_claimed_open_errors` 第 231 行**已经**有 `if not isinstance(step.result, dict): continue` 守卫；R4 还专门为「负数 open_errors」加了守卫（test_teamb_verifier_negative_audit_count_rejected）——唯独漏了「非 dict」这条同类畸形输入在 verifier 侧的守卫。verifier 第 224 行注释本身写着「audit blew up; do not crash the verifier」，但 `.get` 那行恰好落在 try 外，使该意图对非 dict 失效。
- **复现**（独立脚本，4 种返回值全崩）：
  ```
  [list] CRASH -> AttributeError: 'list' object has no attribute 'get'
  [None] CRASH -> AttributeError: 'NoneType' object has no attribute 'get'
  [ str] CRASH -> AttributeError: 'str' object has no attribute 'get'
  [ int] CRASH -> AttributeError: 'int' object has no attribute 'get'
  ```
- **诚实严重度=LOW**：**生产 `audit_project`（mcp_server/tools.py:27-42）恒返回 dict 且 `open_errors=len(...)` 为 int**，所以正常 product 路径触发不到；只有「畸形/恶意/自定义 registry handler」能触发。属于**防御一致性缺口 / 鲁棒性 gap**，不是活数据 bug。值得修因为(a)与已加固的 worker 侧明显不对称是疏漏，(b)verifier 自述意图就是「绝不崩」，(c)修复=1 行类型守卫，非打补丁。
- **已修**：`verifier.py` 在 `.get` 前加 `if not isinstance(result, dict): return None, "audit-malformed"`，镜像 worker 侧守卫。加参数化回归测试 `test_teamb_verifier_non_dict_audit_does_not_crash`（list/None/str/int 四种均不崩、不报 deterministic-audit）。multi_agent 套件 46→50 全绿，ruff + mypy 绿。

---

## R4 新代码端到端验证（4 问全部在真实 session 路径过，确认口径正确）

均通过**完整 `session.run` + 真 content root（种了 1 个 dangling-ref，真 audit=2）+ 真 audit_project**，非夹具手设整数：

1. **① 诚实 worker（中文答案、真实声称数=审计数）现在真不被误判** ✅
   offline ReAct 双子先调 `audit_project`（真值 2），再写中文 Final Answer「发现 N 个待修复错误」。worker 的 `open_errors` = **真 int 2**（取自 `AgentStep.result`，不是旧的 `.count("error")`=0）。verifier 独立审计=2 → **pass, delta=0**。这正是 R4 修的 HIGH，在真实路径确认已修。

2. **② 撒谎 worker（声称 0 实际 N）真被抓** ✅
   worker 从不审计、prose 谎称「0 open errors」。它没调 audit → `open_errors=None`；verifier 独立审计真 content（找到 2），prose 兜底解析出声称 0，**fail delta=2**。端到端在真 content root 上抓住。

3. **③ None（没调 audit）处理对、不被当 0** ✅
   - 诚实非审计 worker（repair_01，没审计也没报数）→ `open_errors=None` → verifier 报「no verifiable error-count claim」**pass**（不当 0、不假 fail）。
   - 撒谎的 None（声称 0 但没审计）→ 走 prose 兜底解析出 0 → 仍 **fail**。两条都对。

4. **④ delta==0 不过严** ✅（代码分析 + e2e 双证）
   - **代码分析**：session 内 worker 与 verifier 调**同一个** registry 的**同一个确定性 `audit_project`**（对**同一份 content**）。`propose_fix`(tools.py:160) 明确「Read-only with respect to content files」——**无任何 agent 写 canon**。所以两次审计之间 content 不变，确定性审计恒等 → 诚实 worker 必精确相等，delta==0 是**正确**而非过严。±1 容差只用于 prose 解析这条近似路径，分层正确。
   - **我构造的「divergent」反例**（worker 见 5 / verifier 见 6 → fail）**在真 product 不可能发生**：它要求给 worker 和 verifier **不同的 registry**返回不同数；真 session 共享一个 registry。所以那不是误判，是我人为破坏前提。

**额外确认：AgentStep.result 在所有 provider 路径都填了** ✅
`react.py` 全项目只有一个 `ReActAgent.run`；`result=structured_result` 在**工具执行边界**（registry.run 之后，第 312 行）填，与 LLM provider 无关——任何 provider 走到工具调用都填。第 251 行「no action」nudge 步 result 默认 None（正确，没调工具）。无第二条绕过此字段的 agent loop。

**R4 其他改动也验过**：
- 负数 open_errors 被拒（既有 test 绿；我的非 dict 测试与它互补）。
- `_parse_subtasks` 角色校验（缺 repair_proposal → degraded）、`_coerce_allowed_skills` 字符串规整（不炸成单字符）——既有 test 绿，逻辑读过无误。

---

## 一条 LOW 观察（非 bug，记录）
`orchestrator.post_task_assignments`（orchestrator.py:127-131）构造 `TaskAssignPayload` 时**不传 `max_steps`**，且 `SubTask` dataclass（:50-57）也不带该字段——所以经分解路径派发的 worker `max_steps` 恒为默认 4，无法由 LLM 分解结果调节。`max_steps` 协议字段只在**直接构造 `TaskAssignPayload`** 的路径（如 test_teamb_max_steps_protocol_field_is_live）才活。属于「orchestrator 路径上该旋钮未接线」，非正确性 bug——分解出的 worker 仍正常跑、口径不受影响。若日后要让分解可控步数预算，可在 `SubTask` 加 `max_steps` 并在第 127 行透传。不建议本轮改（超范围、无现实痛点）。

---

## 末轮 sign-off
我**端到端攻过并确认 OK** 的点（均在真实 `session.run` + 真 content root + 真 audit_project，非手设夹具）：
- 真 multi-agent 群（orchestrator/diag/repair/verifier 四独立类、独立 agent_id、独立 ReActAgent transcript、黑板 SQLite 中介 hand-off）—— 非搭壳，结构成立。
- **verifier 口径现在正确**：诚实 worker 不被误判（delta=0 pass）、撒谎 worker 被抓（fail）、None 不被当 0、delta==0 在确定性路径是对的（无 canon 写入使两侧审计恒等）。
- AgentStep.result 在工具执行边界填，provider 无关，无旁路。
- 不静默降级红线守住：分解降级有 `decomposition_degraded` + 中英 WARNING；worker 崩有 failed task_result；未路由 worker 有 UNROUTED 记录；verifier 无 ground-truth 时诚实 `needs_more(-1)` 不伪造 pass。

唯一新发现=上面那条 LOW 的非 dict 崩溃防御缺口，**已修 + 回归测试 + lint/mypy 绿**。四轮修复后这块已相当扎实，本轮无 HIGH/MID。
