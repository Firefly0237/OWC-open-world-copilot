# R5（末轮）· 赛博极客（prompt 工程 / agent 实现验证）报告

被测：HEAD=9ecf555（R4 fixes）分支 feature/agent-pipeline-enhancements。
方法：读源码 + 4 个 $0 离线攻击脚本 + 跑相关测试（80 passed）。console=GBK，中文 mojibake，但断言/标记均 ASCII，可读。

---

## 结论速览

- **1 个 MEDIUM 真 bug（新发现，可复现）**：`assist/sweep.py` + `assist/contradiction.py` 的 `semantic_used`
  在「语义模型运行期降级」时**对用户撒谎**（声称 bge-m3 已启用，实际跑的是 hashing stub）。
  这是 R4 修 VectorRetriever 时**漏修的同一类快照模式**的两个下游消费者——但与 R4 Team-C 的预判
  「init-time feature gating、较低」**不符**：它是 detect()/sweep() **运行期中途**降级、且**直达用户工作单 + app API**，
  踩了项目第一红线「不静默降级/不自欺」。严重度 MEDIUM（前提较窄、易修、非数据损坏/非安全洞）。
- **R4 新代码（react.py / verifier.py / workers.py）压测：全部诚实有效，无回归。**
- **前几轮我报的 allowed_skills 执行期拦截、orchestrator pass-2 诚实化：确认无回归。**
- FailoverProvider `.model` 缓存键取 primary：经分析为**有意设计/LOW**，非 bug（见下）。

---

## 【MEDIUM】sweep/contradiction 在语义模型运行期降级后谎报 semantic_used=True（真 bug）

### 根因
`SemanticEmbedder`（auto 模式）懒加载：构造时 `model_id='st:bge-m3'`、`degraded=False`；
首次 `embed_many` 若模型加载失败（离线首跑 / 模型名错 / gated / OOM），则
`degraded=True`、`model_id` 翻成 `hashing-*`（embedding.py:104-117，有 warning，机读 `degraded` 标记）。

两个消费者用**构造期快照**判 semantic，但**报告期只查引用非 None**，不重新查 live 后端：
- `assist/contradiction.py:99` `self.embedder = embedder if _is_semantic(embedder) else None`
  （构造期，此时 `st:` → 保留引用）；`:111` `semantic_used=self.embedder is not None`
  （detect() 期，此时引用仍非 None，但 `_semantic_candidates`(:215 embed_many) 可能已中途降级）。
- `assist/sweep.py:202` 同样构造期快照；`:321` `semantic_used=self.embedder is not None`
  （sweep() 期，`_semantic_scores`(:334/337 embed_many) 已中途降级）。

对照：R4 已把 `VectorRetriever.is_semantic` 改成 **live property**（vector.py:91-97 每次读查 embedder），
所以 `inspiration/retrieval.py:48` 读 `self.vector.is_semantic` 是**诚实的**（已验证 OK）。
这两个 `assist/` 消费者**没**走 live 路径——R4 的修复没覆盖到它们。

### 证据（可复现）
- `scratchpad/repro_r5_snapshot_lie.py`（contradiction）→ 输出：
  `embedder.model_id=hashing-1024 (degraded=True)`、`report.semantic_used=True`、
  `_is_semantic(emb) now=False` → `>>> LIE CONFIRMED`。
- `scratchpad/repro_r5_sweep_lie.py`（sweep）→ 输出：同样 degraded=True 但 `semantic_used=True`，
  且**人审工作单**那行走了 `report.semantic_used` 的 True 分支 →
  `render_sweep_markdown` 打印「语义近似（向量）：已启用（bge-m3，阈值 0.99）...」（sweep.py:438-444）。
  实际后端是 hashing。**这就是"静默降级被当成功展示给人看"。**
  - 触发条件细节：theme 词须**不**词面命中任何对象（否则对象不进 `pending`，`_semantic_scores`
    不会 embed，也就不触发降级——我第一版 repro 词面全命中，degraded=False，**不是 lie**，
    诚实记录这一点以免夸大）。
- 传播面：`app/actions.py:1433`（sweep）、`:1484`（contradiction）把 `semantic_used` + `markdown`
  回传给 app/API 层 → 谎言直达 UI，不止内部字段。

### 真 bug vs 设计——诚实标注
**真 bug**，踩「不静默降级/不自欺」红线。但严重度 MEDIUM 不是 HIGH，因为：
1. **常见离线 $0 路径是诚实的**（已验证）：HashingEmbedder 构造期 `_is_semantic=False` →
   embedder 置 None → `semantic_used=False`。谎言**只**发生在「装了 [semantic] extra +
   运行期模型加载失败」这条较窄路径。
2. 是状态标志误报，非数据损坏、非安全洞、不产错误 canon。
3. 机读 `degraded` 标记**已存在**，修复很轻：报告期用 live 后端判（与 R4 VectorRetriever 同款），
   例如 `semantic_used = self.embedder is not None and _is_semantic(self.embedder)`，
   或读 `getattr(self.embedder,'degraded',False)`。happy-path 测试
   （test_sweep_and_brief.py:203 assert semantic_used is True）在不降级时仍 live=True，不会被破。
4. 值不值得修：**值得**，因为它正是项目花了 R4 一整轮去修 VectorRetriever 的同一类 honest-failure
   缺陷，只是漏了 assist/ 两个消费者；作品集对外宣称「不静默降级」，这里留个反例不一致。
   （工程 persona 的 `scratchpad/r5_eng_warming.py` 似乎也在查同一处——建议合并到一条修复。）

---

## R4 新代码压测（重点）——均 PASS，无回归

### ✅ A. react.py 新增 `AgentStep.result` 字段在所有路径正确
- 成功且 dict 结果 → `result=<full dict>`（react.py:277-278）；**错误步 / 非 dict 结果 → result=None**
  （:274 初始化 None，except 分支不赋值）。
- `result` 来自 `self.registry.run()`，**与 provider 无关**——mock/real/failover 路径都经同一行，
  不存在「某 provider 路径没填 result」的隐患（brief 问点已澄清）。
- **观测截断与 result 解耦**：`observation` 被 `_OBSERVATION_CHAR_LIMIT=4000` 截断（带显式
  `… [truncated N chars]` 标记，**非静默**），但 `result` 保留完整结构化 dict。实测：5000 字 padding
  的 audit 结果 → observation len=4024 带 truncated 标记，`result['open_errors']=7` 完整。
  → 下游 `_extract_claimed_open_errors` 读 `step.result`（workers.py:233）拿到真值，**不依赖被截断的
  observation 串**——这正是 R4 引入 result 字段的目的，落实到位。
- parse 鲁棒：幻觉 `Observation:` 被切（react.py:354）；畸形 `Action Input:` → `{}` 不崩
  （:378-379）；`Final Answer:` 正确解析。

### ✅ B. verifier delta==0（确定性路径）不会因合法场景误判 fail
- `_compute_verdict`：`tolerance = 0 if source=="deterministic-audit" else 1`（verifier.py:313）。
- **攻击：合法场景下 delta==0 会不会过严误 fail？** → 不会。worker 与 verifier 都调同一个确定性
  `audit_project`；二者之间唯一的写操作 worker 是 `propose_fix`（builtin.py:112 `PROPOSES_PATCH`，
  「Stores PROPOSALS only — never writes canon」）+ `quality_harness`（:135 `READ_ONLY`）——
  **都不改 audit 读的 canon**。所以 worker audit 与 verifier audit 之间审计态不变 → 诚实 worker 的
  claim 与 verifier 真值**精确相等** → delta==0 → pass。`tolerance=0` 是**正确**的，没有合法的
  非确定性漂移源。单 session 内 verifier 紧跟 worker 同进程跑，无人审插入。
- worker 没调 audit 时：`_extract_claimed_open_errors` 返回 None（workers.py:227-238，
  guard 了 bool/非 int/负数）；verifier 端 `worker_claimed_errors is None` → 走「无可反驳的 claim →
  报告真值 + PASS 并显式标注」（verifier.py:303-309），**不**伪造 fail，也不把缺席读成 0。诚实。
- audit 畸形/不可用：`_deterministic_verify` 对 None/bool/负/SkillError/Exception 全部降级到
  LLM-answer 路径，仍无可解析则诚实 `needs_more(-1)`（verifier.py:206-238）——R3/R4 已覆盖，无回归。

### ✅ C. workers.py 取真实结构化 audit 结果
- `_extract_claimed_open_errors` 扫 `step.result`（结构化、截断前）而非 `answer.lower().count("error")`
  旧启发式（对中文「错误」恒为 0 的老 bug 已根治）；取**最后一次**成功 audit 的计数；
  guard 了 bool/非 int/负数。None 语义=「无 audit-backed claim」≠0，诚实。

### ✅ D. allowed_skills 执行期拦截（我前几轮报过）——无回归
- `ScopedSkillRegistry.run`（skill_scope.py:42-50）对越界 skill 在 **dispatch 期**抛 SkillError，
  不只是 manifest 隐藏；`get`/`manifest`/`__contains__` 全 filtered；`allowed=None` 返回 base
  向后兼容。verifier 硬 scope = {audit_project,list_issues}；worker 用 per-task allowed。

### ✅ E. orchestrator pass-2 诚实化（我前几轮报过）——无回归
- orchestrator.py 无任何 `ReActAgent` 实例化；pass-2（:289-314）是普通 `gateway.complete` +
  更严 JSON-only prompt（非表演式 meta-agent）。pass-1 失败→pass-2 恢复=非 degraded；
  双失败→静态回退 + `logger.warning [DECOMPOSITION DEGRADED]`，degraded 标志贯穿到 synthesis
  与 report 字段，**不静默**。`_parse_subtasks` 还校验必须含 diagnosis+repair_proposal 两角色
  （R4 LOW-1 已修，:438-446），缺角色 → degraded=True 触发回退。

---

## FailoverProvider `.model` 缓存键取 primary —— 经分析为设计/LOW，非 bug

- `FailoverProvider.model` 返回 `self.primary.model`（resilience.py:59）；gateway 缓存键
  `model = getattr(provider,"model",None) or tier`（gateway.py:231）。
- **brief 问点**：已 failover 到 backup 时，缓存键/span 仍报 primary 而非实际 backup？——是的，会。
- **诚实评估**：这是**有意设计 + 文档化 tradeoff**（resilience.py:50-58 明确说明），且：
  1. resilience 整套是 **opt-in env-gated**（`OWCOPILOT_FALLBACK_MODEL`），默认 real-mode 不启用，
     字节级不变。
  2. `gen_ai.request.model` 的 OTEL 语义=「请求的模型」——请求**总是先打 primary**，报 primary 对
     request 属性是可辩护的（response.model 才是实际应答方，本项目未用该属性）。
  3. 唯一真实风险：primary 持续宕机、每次都 failover 到 secondary 时，secondary 的应答被缓存在
     primary 的 model key 下，primary 恢复后可能取到 secondary 的缓存答案。但 failover 是**同一逻辑
     请求的同任务兜底**，两模型对同 (tier,system,user) 都在产同任务答案，且这是 opt-in 容灾路径下的
     边角——非默认、非数据损坏。
  - 结论：**不报 bug**。若要更严谨可改成「failover 后用实际命中的内层 model 重算 key」，但属增强非缺陷修复，
    不值得在末轮当 bug 提。诚实记录供编排者判断。

---

## 末轮 sign-off

我这轮攻过并确认 OK 的点（「真推理非搭壳 / 降级不静默 / 约束真注入」）：
- ReAct 解析对畸形输出鲁棒、observation 截断带显式标记非静默、`result` 字段 provider 无关且与截断解耦。
- verifier 确定性路径 delta==0 正确（propose-only 不改 canon → 无合法漂移），无 claim/畸形 audit 均诚实降级不伪造 pass。
- workers 取真实结构化 audit 计数，None 语义诚实。
- allowed_skills 执行期沙箱、orchestrator pass-2 诚实第二次 gateway 调用（非表演 agent）——前轮修复无回归。
- inspiration/retrieval.py 读 VectorRetriever live `is_semantic`——诚实，未撒谎。
- 常见离线 $0 路径 semantic_used=False 诚实。

唯一真 bug：**sweep/contradiction 的 `semantic_used` 运行期降级谎报（MEDIUM）**——R4 修 VectorRetriever
时漏修的同类快照模式两个下游消费者，直达用户工作单/API，踩第一红线，建议修（live 重判，约 2 行/处）。

测试证据：tests/test_multi_agent.py + test_agent_react.py + test_sweep_and_brief.py +
test_embedding_degrade_marker.py = **80 passed**。攻击脚本：repro_r5_snapshot_lie.py、repro_r5_sweep_lie.py（均 EXIT=0、LIE CONFIRMED）。
