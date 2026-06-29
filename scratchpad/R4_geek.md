# R4 · 赛博极客（prompt/agent 实现验证）复测报告

被测：HEAD=73d4d04（R3 fixes）分支 feature/agent-pipeline-enhancements。
方法：读源码 + 6 个独立攻击脚本（probe1–6，全 $0 离线）+ 跑相关测试套件（61 passed）。
console 是 GBK，中文 mojibake 但所有断言为 ASCII bool，可读。

---

## 结论速览
R3 在我领域（orchestrator pass-2 / ScopedSkillRegistry / lessons 措辞改写 / verifier 确定性路径）
的三项改动**全部诚实有效**，且对畸形输入鲁棒、降级不静默。压测未发现 R3 引入的回归。
另发现 **2 个 LOW（真 bug，但非安全/非静默降级，影响有限）** 和若干「验证 OK」点。无 CRITICAL/HIGH。

---

## 一、R3 改动验证（重点压测，均 PASS）

### ✅ A. orchestrator pass-2 = 诚实第二次 gateway.complete（非表演式 meta-ReActAgent）
- **代码确认**：orchestrator.py 无任何 `ReActAgent` 实例化、无 `self.agent` 属性；
  pass-2（L289-314）是一次普通 `gateway.complete` + 更严格 JSON-only prompt。
  所有 `ReActAgent` 字样只出现在解释「已移除」的注释里（L28-32）。
  测试 `test_p3_7_...` 断言 `not hasattr(session.orchestrator, "agent")`——成立。
- **pass-1 失败→pass-2 恢复**：probe(test_rt3_7) 已覆盖 prose→cleanJSON 恢复=非 degraded。✅
- **新攻击：pass-1 prose + pass-2 也 prose（双失败）**（probe1#1）→ decompose calls=2、
  degraded=True、回退静态模板并 `logger.warning` 打印 `[DECOMPOSITION DEGRADED]`。**不静默**。✅
- **新攻击：pass-2 抛异常**（probe1#2）→ `except Exception` 捕获、`logger.warning`、
  degraded=True、不崩溃。**安全降级**。✅
- 证据：probe1.py PROBE 1/2；source orchestrator.py:282-317。

### ✅ B. ScopedSkillRegistry 执行期拦截真生效，且无误拦合法工具
probe2 PROBE 5 / probe5 PROBE 11 全绿：
- 越界 `propose_fix` 在 `run()` 期被 `SkillError` 拒绝（不仅 manifest 隐藏）。✅
- 大小写/前后空格变体（`Propose_Fix`/` propose_fix`/`PROPOSE_FIX`）均拒绝，无绕过。✅
- `get()` 同样 gated；`manifest(allowed=更大集合)` 被硬 scope 求交，永不广告越界工具。✅
- `__contains__` 诚实（allowed∩base）；allowed=None 直接返回 base（向后兼容）。✅
- 空 allowed 集 = 全拒（deny-by-default）；allowed 含 base 没有的 ghost_skill → 透传 base 的
  unknown 错误、`__contains__`=False。✅
- **合法工具未被误拦**：probe5 中 in-scope `audit_project` 正常执行（teamb 测试也覆盖）。✅
- 源码层面 ReActAgent 只调 registry.`manifest`/`run`，verifier 只调 `run`/`__contains__`，
  全部被 proxy 覆盖——无未代理方法导致 AttributeError 的隐患。✅

### ✅ C. lessons.py critic-lesson 措辞改写（general + dimension 两路）
probe2 PROBE 6 + probe6 **真 SQLite 端到端 round-trip** 全绿：
- dimension lesson：`生成时请着重提高`→`评判时请着重核查` ✅
- general lesson：`生成时请整体提高`→`评判时请整体核查`（R3 新补的那条）✅
- 无 generation-side 措辞泄漏进 critic block（leaked=[]）。✅
- dim-hint 仅对 non-general 出现；缺 dimension 键默认 general 且不加 dim-hint。✅
- 末尾 severity→blocker 指令存在。✅
- round-trip 证明 dimension 真落库（save_lesson）、真取回（get_lessons_for_type）、
  改写在真实持久化行上生效——不是只对内存 dict 有效。✅

### ✅ D. verifier `_deterministic_verify` 在 audit 不可用/畸形时安全降级
probe3 PROBE 7：对 7 种畸形 audit 输出（open_errors 为 bool/str/None/float、缺键、
SkillError、generic Exception）**全部**正确 → det 路径返回 None → 回退 LLM-answer 路径 →
离线 MockProvider 无法产出可解析答案 → 诚实 `needs_more`(-1)。**零一例伪造 pass**。✅
- `det_errors is not None` 才作判据；bool 被显式排除（`isinstance(open_errors, bool)`）。
- 证据：verifier.py:200-225 + probe3 PROBE 7。

---

## 二、新发现（LOW，真 bug，非安全/非静默降级）

### 【LOW-1】_parse_subtasks 不校验角色覆盖：LLM 返回「只有 diagnosis、无 repair」被当作非降级
- **根因**：`_decompose_goal` 的 system prompt 明确要求「Always include at least one diagnosis
  AND one repair_proposal subtask」，但 `_parse_subtasks`（orchestrator.py:377-396）只要解析出
  ≥1 个 dict 子任务就返回 `degraded=False`，不检查是否同时含 diagnosis+repair_proposal。
- **后果**：LLM 若只吐一个 diagnosis 子任务，session 只跑 diag、**根本没有 repair 提案环节**，
  且 `degraded=False` → 没有任何告警/标记。这是「指令未被遵守却静默放行」。
- **证据**：probe1 PROBE 3 → `degraded=False, n=1, workers=['diag_01']`。
- **真 bug vs 设计**：真 bug（轻）。属内容/编排完整性，不是安全洞，也不会产出错误答案——
  只是少了 repair 半边且无标记。修法很轻：parse 后若缺某必需 role，置 degraded=True 触发 pass-2，
  或在 synthesis 标注「decomposition incomplete: no repair subtask」。
- 注：与设计前提里「honest-failure 不静默降级」红线**轻微抵触**，故值得记，但严重度 LOW。

### 【LOW-2】allowed_skills 为字符串时 list() 逐字符炸开（fail-safe，不泄漏，但产生无用 worker 且不标 degraded）
- **根因**：`_parse_subtasks` 用 `list(item.get("allowed_skills", []))`（orchestrator.py:383）。
  若 LLM 把 `allowed_skills` 写成裸字符串 `"audit_project"`，`list("audit_project")` →
  `['a','u','d','i','t',...]`（13 个单字符），成为该 worker 的 allowed 集。
- **后果**：worker 的 scoped registry 里没有任何真实工具名匹配这些单字符 → **全部工具被拒** →
  worker 空转到 max_steps。`degraded=False` 不标记。
- **安全性**：probe5 PROBE 11 实测 = **FAIL-SAFE（deny-all）**：audit/propose handler 调用数均 0，
  即 ScopedSkillRegistry 让畸形 allowed_skills **拒绝过度而非过度放行**，安全边界不破（无 canon/patch 泄漏）。
- **真 bug vs 设计**：真 bug（轻、cosmetic+鲁棒性）。非安全洞（恰好 fail-safe），但「悄悄造了个废 worker
  还不标 degraded」同样轻微踩 no-silent 红线。修法：`allowed = item.get("allowed_skills"); 仅当
  isinstance(list) 才用，否则视为缺失/degraded`。
- 证据：probe4 PROBE 10（逐字符炸开）+ probe5 PROBE 11（fail-safe 验证）。

---

## 三、验证 OK、并非 bug 的点（避免误报，列明）

- `_extract_error_count` 正则鲁棒（probe3 PROBE 8）：EN/中文计数、0、多位数、首个匹配优先都正确；
  唯一 [DIFF] 是我测试期望写错（"found 4 errors"→4 本就是对的）。此乃 audit 不可用时的兜底启发式，
  设计上即近似，无问题。
- `_compute_verdict` ±1 容差边界正确（probe3 PROBE 9）：delta≤1 pass、≥2 fail，对称无漂移。
- pass-2 触发条件正确：仅 pass-1 degraded 才二次 complete；非降级路径不会浪费第二次调用。
- 嵌套方括号字符串 `description:"fix [ref]"` 解析正确（rfind 取最后一个 `]`）（probe1 PROBE 4）。
- `worker_id` 默认成 `{role}_01`（如 `repair_proposal_01`）虽不在 session 工作表里，但走 ③ UNROUTED
  诚实失败记录，非静默——已被 R1/R2 处理，非新 bug。
- orchestrator pass-2 strict prompt 真的更严（要求以 `[` 开头 `]` 结尾、禁 fence/prose），
  是「再问得更狠」的诚实重试，不是换皮。

## 四、非我领域但顺手记录的既有小瑕疵（非 R3 回归，仅诚实备案）
- `save_lesson` 的 DB 列 `false_pass_count` 是「每次 upsert +1」（起始 1），**不等于** lesson_text 里
  写的「历史有 N 次」（N=真实 false-pass 计数，由 `extract_lessons_from_report` 算）。probe6 见
  retrieved 行 count=1 而文本写 4/3。**不影响 prompt 正确性**（模型看到的是文本里的真数 N），
  纯属列与文本口径不一致，且是 R1/R2 期就存在、非 R3 改动。严重度可忽略，留作备案。

---

## 测试证据
- tests/test_multi_agent.py：39 passed
- tests/test_lesson_archive.py：14 passed
- tests/test_reviewer_calibration.py：（合并跑）总 61 passed
- 6 个独立攻击脚本全部按预期（probe1–6）。
