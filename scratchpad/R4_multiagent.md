# R4 · multi-agent 深审（身份：multi-agent/sub-agent 技术极客）

被测：`F:\openworld\src\owcopilot\multi_agent\*`（HEAD 73d4d04，R3 fixes 后）
基线：`tests/test_multi_agent.py` 39 passed；`ruff`/`mypy` 全绿。

---

## 【高】真 bug · 回归：deterministic verifier 比较的是「两个不同的量」→ 真实路径上诚实 worker 被误判为撒谎（false fail）

**这是 R3 新实现 `_deterministic_verify` 引入的语义回归——R3 修好了「verifier 永远 needs_more」，但把比较的另一端接错了。**

### 根因
verifier 比较 `worker_claimed_errors` vs 审计 `open_errors`，但这两个数压根不是同一种东西：

- verifier 端（`verifier.py:221`）= `audit_project` 返回的真实 issue 计数（ground truth，整数）。
- worker 端（`workers.py:120` → `_count_errors_in_answer`，workers.py:202-209）= **`answer.lower().count("error")`**，即 worker 最终答案里 Latin 子串 "error" 出现的次数。

后者根本不是「worker 声称发现了几个错误」，而是一个随答案啰嗦程度漂移的子串计数；且对中文答案恒为 0（中文用「错误」，不含 Latin "error"）。`_compute_verdict`（verifier.py:279）用 `delta = abs(audit_count - 子串计数)` 判 pass/fail，于是把两个不可比的量做了减法。

### 证据（端到端，走真实 `MultiAgentSession` + 真 `OfflineReactProvider` + 真 `audit_project`）
`scratchpad/e2e_session.py`（世界种 4 个 dangling-ref，audit 实测 8 个 open_errors）：

```
=== Worker summaries ===
  diag_01    role=diagnosis        open_errors(field)=0 stop=finished
  repair_01  role=repair_proposal  open_errors(field)=0 stop=finished
=== Verifier verdicts ===
  verdict=fail  audit_found=8
    Verifier independently found 8 open error(s) [deterministic-audit] but worker claimed 0. Delta=8 exceeds tolerance.
  verdict=fail  audit_found=8  (同上)
```

- diag worker **诚实地跑了 audit**，它的离线脚本（`agent/offline.py:139-145`）甚至在中文 Final Answer 里写「一致性审计发现 N 个待修复错误」；但 `open_errors` 字段因子串计数 = **0**。
- verifier 独立审计得 8 → 判 **fail**「worker claimed 0」。**诚实 worker 在真实产品路径上被打成撒谎者。**
- 连 repair worker（它根本不声称任何错误计数）也被一并判 fail。

补充单元证据 `scratchpad/repro_verifier2.py`：
- **false fail**：worker 答案叙述 2 个真错误但用了 7 次 "error" 子串 → 字段=7，audit=2 → `delta=5` → **fail**。
- **false pass**：clean 世界（audit=0），worker 谎称「catastrophic error breaks main quest」但只含 1 次 "error" → 字段=1 → `delta=1` → **pass**（谎言因为只比整数、不比内容而隐形）。

### 为什么测试没抓到
`test_teamb_verifier_catches_worker_underreport` / `..._real_verdict_offline` 用 helper `_post_task_result`（test_multi_agent.py:1082）**直接把 `open_errors=` 设成手挑的整数**，绕过了真实 worker 唯一会走的 `_count_errors_in_answer` 路径。所以「抓撒谎」的能力只在被构造好的测试夹具里成立，真实 session 里这个比较是坏的。

### 诚实标注
**真 bug（回归）**，非已兜底。R3 把 verifier 从「永远 needs_more」修成了「会出真 verdict」，但 verdict 的可信度建立在一个错误的等价假设上（worker 字段 == 审计计数）。红线相关：这是「自欺」——report 会用一个看似权威的 `[deterministic-audit]` 措辞输出系统性误判。

### 修复方向（择一）
- 让 worker 也走确定性 `audit_project` 取真实 `open_errors` 整数（与 verifier 同口径），而不是子串计数；或
- worker 不再自报 `open_errors`，verifier 只做「我独立审计得 N，worker 答案是否与 N 一致」的语义核对；或
- 至少：verdict 措辞不要声称比较了「worker claim」，因为被比较的不是 worker 的 claim。

---

## 【低】真 bug · 边界：审计返回**负** `open_errors` 未被防住，直接当真 verdict 透传

### 根因
`_deterministic_verify`（verifier.py:221-225）防了 `bool` 和非 `int`，但没防 `int < 0`。

### 证据 `scratchpad/repro_verifier.py` CASE 3：
```
open_errors=-5 (negative) -> verdict=fail verified=-5 src_in_rationale=True
```
其余畸形（缺 key / None / bool / float / str）都安全降级到 `needs_more`（verified=-1），唯独负整数穿过，且 `open_errors_verified=-5` 与「target 不存在」哨兵 `-1`（verifier.py:111,167）语义撞车。

### 诚实标注
真 bug 但**低危**：真实 `audit_project` 不会返回负数（`len(...)`），需畸形/恶意工具才触发。一行 `or open_errors < 0` 即可收口。属防御完整性问题，非当前可触达缺陷。

---

## 【设计/已兜底，非 bug】`delta <= 1` 容差偏松——但有意

brief 问「delta<=1 算 pass 是否过松」。`scratchpad/repro_verifier.py` CASE 2 证实：audit=3 / worker_claimed=2（少报 1）→ **pass**；over-report 1 也 pass。

标注为**设计取舍**而非独立 bug：它本身只是容差宽窄问题。但它**放大了上面的高危 bug**——正是这个 ±1 窗口让 CLI 冒烟测试（只种 1 个错误 → audit≈1、worker 字段=0 → delta=1 → 蒙混 pass）看起来正常，掩盖了 metric 不可比的根因。**修了高危 bug 后**再单独评估是否收紧到 delta==0。

---

## 验证 OK 的点（确认「真 multi-agent」非搭壳）

1. **独立 worker / 各自 ReActAgent**：`DiagWorker`/`RepairWorker`/`VerifierAgent` 各自 class、各自 `agent_id`、各自 `ReActAgent` 实例（workers.py:63 每实例 fresh，verifier.py:73）。`test_p3_1_*` 验证 4 个 distinct agent + transcript 不共享。真。
2. **黑板**：`AgentBlackboard` 真 SQLite append-only 表，payload 写后不可变（仅 `status` 列可改，blackboard.py:186-195）。worker 经 `claim_task` 从黑板领取，不走函数返回值。真。
3. **乐观锁**：`claim_task`（blackboard.py:124-137）条件 UPDATE `WHERE status='pending'`，`rowcount==0` 即判他人先抢。`test_p3_3_claim_prevents_double_claim` 通过。真。
4. **独立 verifier**：verifier 只读黑板上的 `task_result.final_answer`，从不碰 worker 的 `agent._transcript`（架构隔离，非 prompt 约束）。gateway 同实例但推理种子来自自己独立的 `audit_project` 调用。真（除上文 verdict 比较口径 bug 外，隔离本身成立）。
5. **`ScopedSkillRegistry` 执行期拦截（不被绕过、不误拦）**：
   - 不绕过：ReActAgent 只经 `.manifest()`/`.run()` 触达 registry（react.py:160,266），二者皆被代理；`.get()` 也加了门（skill_scope.py:53-59）；无任何 `registry.get(name).run()` 旁路。亲测 `scratchpad`：越权 `propose_fix` 经 `.run()` 和 `.get()` 双双 DENIED。
   - 不误拦：in-scope `audit_project` 正常执行返回真结果；`manifest()` 求交集（skill_scope.py:76）不会广告越权工具；`allowed=None` 直接返回 base（向后兼容）。`test_teamb_allowed_skills_enforced_at_execution` + `..._allows_in_scope_skill` 通过。真。
6. **死字段已清干净**：`confidence` 字段全包搜索为 0（仅 messages.py:69 注释里出现单词「confidence」）；`max_steps` 真接线生效（workers.py:115 读 `assign.max_steps` → `_make_agent`；`test_teamb_max_steps_protocol_field_is_live` 验 `step_count==1`）。真。
7. **audit 不可用安全降级**：缺 `audit_project` / 工具抛异常 / `SkillError` → 回退 LLM-answer 路径，无解析则诚实 `needs_more`（never fabricated pass）。CASE 4 + CASE 3 多数畸形分支验证通过。真（仅负数漏网，见低危条）。
8. **orchestrator pass-2 二次 gateway.complete**：pass-1 解析失败才触发 pass-2 严格 JSON 重问，pass-2 的 `complete`+`_parse_subtasks` 整体包在 try/except，抛异常则落静态模板且 `degraded=True`（非静默）。`test_rt3_7` 验证 pass-2 真做事且恰好 2 次调用。真。
9. **不静默降级**：分解降级 `decomposition_degraded` 一路透传到 report 字段 + synthesis 文本（orchestrator.py:421-426）；worker crash / unrouted / needs_more 均显式落黑板并在 synthesis 标注。`test_rt3_1/2/3/6` 通过。真。

---

## 一句话总结
R3 修对了「verifier 不再永远 needs_more」且 `_deterministic_verify`/`ScopedSkillRegistry`/死字段清理都属真实落地非搭壳；但发现一个【高】回归——verifier 把 worker 的「`open_errors`=答案里 'error' 子串计数」当成 worker 的错误声明，与审计真实计数做减法，导致真实离线路径上诚实 worker 被系统性误判为撒谎（端到端实测 audit=8/worker 字段=0→fail），其「抓撒谎」能力只在绕过真实 worker 路径的测试夹具里成立。
