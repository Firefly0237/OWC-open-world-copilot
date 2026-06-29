# R5（末轮）· 谨慎保守 / 正确性·边界·状态传播 视角

身份：资深 agent 工程师（主循环 / 错误处理 / 并发锁 / 资源 / 边界 / 状态传播 / 异常静默吞）。
最在意「不静默降级、不自欺」「真实落地」。可实跑（`.venv`），区分真 bug vs 已兜底。

环境提示：包名是 `owcopilot`（brief 写的 `openworld` 是项目目录名，非 import 名）。

---

## 结论速览

- **没有发现 HIGH/MEDIUM 真 bug。** 四轮修复后我领域（含 R4 五处新代码）确认稳。
- 找到 **2 个 LOW（真实但低危/已被环境兜底）** 值得记录，**均不建议在末轮强行改**（改了是锦上添花，不改无功能损害）：
  1. 【LOW·真·已被运行时序兜底】task3 的 `contradiction.py` / `sweep.py` 构造期快照 `is_semantic`，理论上运行时降级后 `semantic_used` 会撒谎——但生产装配（ProjectContext）下被 VectorRetriever 的预热兜住，实际不可达。
  2. 【LOW·真·设计可辩护】`AgentStep.result` 新字段使单 agent CLI 的 JSON 输出把**未截断**的完整结构化工具结果（实测 audit ~28KB）也 dump 出来，绕过了 `_OBSERVATION_CHAR_LIMIT=4000`。
- resilience `.model` 透传报 primary（即便已 failover 到 backup）——**确认行为属实，但判为 by-design（request.model 语义）**，非新 bug，详见下。

全量回归：`pytest tests/ -q` → **1371 passed, 2 skipped**（116s）。R4 触及的 6 个 suite 单独跑 102 passed。

---

## 任务3（重点）：构造期 `model_id.startswith("st:")` 快照在运行时降级后会不会撒谎？

### 先澄清范围（brief 列了 4 个文件，实际只有 2 个是「快照」）

- `retrieval/context_pack.py:85` 和 `inspiration/retrieval.py:48`：调的是 `self.vector.is_semantic`——而 R4 已把 `VectorRetriever.is_semantic` 改成 **live property**（vector.py:91-97，每次读 `self.embedder.model_id`）。所以这两个**读的是实时值，不是快照，本身已诚实**。✅ 排除。
- 真正的快照只有：
  - `assist/contradiction.py:99` → `self.embedder = embedder if _is_semantic(embedder) else None`
  - `assist/sweep.py:202` → 同款
  上报 `semantic_used = self.embedder is not None`（contradiction.py:111 / sweep.py:321），且该上报发生在 `detect()`/`sweep()` **跑完 embed 之后**。

### 【LOW · 真 bug，但生产被运行时序兜底】快照式 semantic_used 在「冷 embedder 直注 + 运行时首嵌降级」下会撒谎

**根因**：`auto` 模式的 `SemanticEmbedder` 是惰性的——`model_id` 在首次 `embed_many` 前一直是 `st:...`（embedding.py:76-78）；首嵌失败才降级到 `hashing-*` 并置 `degraded=True`（embedding.py:104-117）。
构造期 `_is_semantic` 快照在「还是 `st:`」时通过 → 保留引用 → `detect()` 内首嵌触发降级 → 但 `self.embedder` 仍非 None → `semantic_used` 仍报 True，**而底层实际用的是 hashing**。这正是 brief 担心的「撒谎 is_semantic」，和 R4 在 vector.py 修掉的是同一类。

**证据（直注冷 embedder，复现成功）** `scratchpad/repro_r5_snapshot_lie.py`：
```
after detect(): embedder.model_id = hashing-1024 (degraded=True)
                report.semantic_used = True   <-- 撒谎
                _is_semantic(emb) now = False  <-- 真实后端
>>> LIE CONFIRMED
```

**为什么严重度只给 LOW（诚实降级判断）**：生产装配里没有「冷 embedder 直注 detector」这条路。所有真实调用点（`app/actions.py:1477,1532`、`compliance/service.py`）传的是 `project.embedder`——一个**共享**embedder，且 `ProjectContext.open()`（project.py:60）在返回前就用它构造了 `VectorRetriever`，其 `__init__→_reindex()` 会对**非空 content_index 立即 `embed_many` 预热**。而 content_index 每个 entity 一行（sqlite.py:1097），contradiction/sweep 又只对 `bundle.entities` 之类的内容嵌入——**有内容可嵌 ⇒ 必有 entity ⇒ content_index 非空 ⇒ open() 内已预热（要降级早降了）⇒ detector 构造时快照已是 `hashing-*` ⇒ 正确置 None ⇒ semantic_used=False（诚实）**。

**实证（用 65-entity 的 seeded 世界走真实 ProjectContext）**：
```
before open: model_id=st:...        degraded=False
after  open: model_id=hashing-1024  degraded=True   <-- open() 内已降级
det.embedder is None? -> True
report.semantic_used = False                         <-- 诚实，未撒谎
```
唯一残留窗口是「空 bundle（0 entity）」——但那时 contradiction/sweep 本就没东西可嵌、findings=0，`semantic_used=True` 纯属无害装饰。

**值不值得修 / 怎么最干净**：这是真实的脆弱不变量（依赖"vector 先预热"这条隐性时序），未来若有人新接一条不经 vector 预热的调用就会真撒谎。修法**很干净且与 R4 vector.py 同构**：把上报从「构造期快照引用是否非 None」改成「运行时实时判定」——即 `semantic_used` 读 `_is_semantic(self.embedder)`（实时查 `model_id`），而不是 `self.embedder is not None`。两处各一行。**我的诚实建议：作为一致性/防御性补丁值得做，但不是末轮必须**——当前生产不可达，且 R4 已判 init-time gating 低危，与之一致。R5 不强改，列为已知 LOW + 给出修法即可。

---

## R4 新代码压测（逐项）

### 1. `agent/react.py` 新增 `AgentStep.result` —— 各路径填充 & None 下游安全？

- **填充正确**：仅在「成功且结果是 dict」时 `structured_result = result`（react.py:277-278）；error 步、非 dict 结果、`parsed.action is None` 的 nudge 步（react.py:250-258，不传 result）一律 None。✅ 与 docstring 一致。
- **下游 None-safe**：`workers._extract_claimed_open_errors`（workers.py:228-238）先 `if not isinstance(step.result, dict): continue`，再校验 `open_errors` 必须是非负 int 且排除 bool。✅ None 安全，恶意/畸形结果也兜住。
- **不破坏既有 step 序列化**：pydantic 字段，默认 None，旧消费者忽略即可。multi-agent 路径只把抽出的 `open_errors`(int) 发到 blackboard（TaskResultPayload），不发 steps，**不受影响**。

  【LOW · 真·设计可辩护】**单 agent CLI 输出膨胀 / 内部字段外泄**：`cli/main.py:720` `**result.model_dump(mode="json")` 会把整个 AgentResult（含每个 step 的新 `result`）序列化。实测一次 `audit_project` 结构化结果 **~28KB**，而喂回 prompt 的 `observation` 被截到 4000 字符——新字段让 CLI 的 `--format json` 输出**每个成功步多出未截断的完整结果**（observation 截断 cap 对输出失效；对 prompt 仍有效，循环只用 observation，所以**不影响 agent 行为正确性**）。这是 R4 顺带扩大的输出面，属设计小瑕疵非缺陷；如在意可在 CLI dump 处 `exclude={"result"}` 或 `model_dump(exclude=...)`。不建议末轮改。

### 2. `multi_agent/verifier.py` —— `delta==0`（deterministic 路径 tolerance=0）会误判吗？

**不会，确认安全。**
- `tolerance=0` 仅当 `source=="deterministic-audit"`（verifier.py:313）。此时 worker 与 verifier **读同一个确定性 `audit_project`**，count 是 `len(issues)`（顺序无关、无时钟/随机），同一 project 同一快照 ⇒ 必然相等。
- 只有 **DiagWorker** 能跑 audit_project（其 allowed_skills 含 `audit_project`）；**RepairWorker 不能**（scoped registry 拒），故其 `_extract_claimed_open_errors` 恒 None → verifier 走「无 claim → pass」分支，根本不进 delta 比较。
- session 流程中 worker→verifier 之间**无 canon 写**（propose-only），不会出现"中途改稿致 count 合法下降"的误判源。
- worker 没调 audit 时 `open_errors=None`，verifier 回退到 prose 解析；再解析不到 → `needs_more`（不伪造 pass）。✅ 全链路诚实。

### 3. `retrieval/vector.py` —— `model_id`/`is_semantic` 改 live property，性能/一致性/并发？

- **无性能问题**：`model_id` property 只是读 `self.embedder.model_id` 这个**字符串实例属性**（HashingEmbedder/SemanticEmbedder 都在 __init__ 设好），不触发任何模型查询/嵌入。`is_semantic` 仅 `_assemble` 每次 build 调一次。
- **重 key 边界正确**：`_reindex`（vector.py:159,184-199）先用 `lookup_model_id` 查缓存，embed 后**再读一次** `persist_model_id`；若降级（key 变了）则丢弃旧 key 命中、按真实后端整体重嵌+重 key，绝不把 hashing 向量持久化到 `st:` key 下污染缓存。逻辑严谨。
- **并发良性**：降级时 `SemanticEmbedder.embed_many` 无锁改 `model_id/degraded`，但多线程都写**同一幂等值**（`hashing-*`/True），竞态无害；矩阵重建发生在单线程构造期。✅

### 4. `llm/resilience.py` —— Failover/CircuitBreaker `.model` 透传，failover 后报 primary？

**行为属实（复现确认），但判为 by-design、非新 bug。**
- `scratchpad`（内联复现）：primary down、调用 failover 到 backup 时——
  ```
  call served by   : ok from qwen-backup
  FailoverProvider.model = 'deepseek-v4-pro'   <-- 报 primary
  CircuitBreaker.model   = 'deepseek-v4-pro'   <-- 链到内层 FO 也报 primary
  ```
- gateway 在调用**前**一次性解析 `model = getattr(provider,"model")=primary`（gateway.py:231），同时用于 cache key 与 telemetry CallRecord.model（gateway.py:245,265）。
- **为何不算 bug**：
  - cache key / `gen_ai.request.model` 取 primary 是**对的**——OTEL GenAI 规范里 `gen_ai.request.model` 是「请求的模型」，调用确实先打 primary；这里也没记录 `gen_ai.response.model`（那才是实际后端，属已知未实现，非新引入）。
  - cache 污染角度真实但**极窄且 opt-in**：需 ①配 failover(env) ②primary 挂 ③backup 成功 ④结果以 primary key 落缓存 ⑤primary 恢复 ⑥同 (tier,system,user,namespace) 请求命中——后果是「拿到 backup 答案以为是 primary」，而二者本就是运营方声明的可互换 fallback。
  - **且这不是 R4 回归**：R4 之前 wrapper 无 `.model`，gateway 会 `getattr(...)→None→model=tier`，primary/backup 同样**坍缩到 tier**（更粗）。R4 加 `.model` 只是把 key 变细到 primary，没把任何东西变错。
- 结论：不改。若将来要更精确，方向是记 `response.model`（实际活跃后端），而非动 `request.model` 透传。

---

## 其他边界确认（顺手攻、确认 OK）

- **content/store.py 写盘边界（traversal）**：唯一把 id 插进文件名的路径是 `_write_json_dir`，已对每个 object_id 走 `_validate_id_chars`（forbidden=`/\.:`）。jsonl/aggregate 写手（relations/event_refs/terms/style_guides）写**固定文件名**，id 只作 JSON 值，**无路径插值 ⇒ 无 traversal**。实测：恶意 entity id `../../evil` → ValueError 拦截；合法 qer 冒号 id `quest:q1:event:e1` → 正常 save + round-trip（进 `event_refs.jsonl`，不当文件名）。✅ R4 修复完整正确。

---

## 末轮 sign-off

我攻了：(A) task3 四个快照消费者——证伪两个（context_pack/inspiration 已 live），证实两个（contradiction/sweep）真撒谎但生产被预热兜底=LOW；(B) AgentStep.result 全路径填充+None 下游+序列化面=正确，仅 CLI 输出膨胀 LOW；(C) verifier delta==0=安全；(D) vector live property 性能/一致性/并发=无问题；(E) resilience .model 透传=确认报 primary 但 by-design 非 bug；(F) store 写盘 traversal=拦截到位、合法冒号 id 不误伤。

**无 HIGH/MEDIUM。** 两个 LOW 均已给诚实定级（真实但低危）+ 最干净修法，**建议不在末轮改**（生产不可达 / 无功能损害 / 与 R4 既有低危判定一致）。全量 1371 tests 绿。我领域确认 OK，可收官。
