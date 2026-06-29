# R5 末轮 · 竞品资深 PM 视角（自欺/注水/伪闭环 / 宣称 vs 实现）

身份：竞争对手公司资深项目经理。视角：功能闭环、demo vs 真实、模块互通、评测对实现是否放水、宣称 vs 实现。
结论先行：**本轮未发现新的真 bug。两个 R4 修复均验证到位。给出末轮 sign-off + 1 条 trivial 文档级观察。**

---

## 一、R4 两个修复回归验证（我上轮揪出的）

### 1. README "seven gates" 少报 → 已真修，count 精确对齐 ✓
- README:115 现写 "verifies **seven gates**"，列举 7 项。
- 实现 `evaluation/acceptance.py` 实际 append 7 个 gate（grep `name="`）：
  clean_world_zero_false_positives / impact_recall_100 / retrieval_hit_rate_gate /
  retrieval_tight_hit_rate_gate / qa_citation_existence_or_refuse /
  seeded_error_detection_gate / tool_selection_accuracy_gate。
- **运行时实测**（`eval-acceptance --workspace …`）：7 个 gate 全部 `passed:true`，count=7，与 README 文案逐项对齐。
- 加分：gate 输出**主动自曝边界**——qa gate 的 details 明写 "does NOT verify entailment"，seeded gate 诚实报 "rules_covered: 20/29" 并列出未覆盖规则。这是反自欺的正面样板，非注水。
- 判定：**真修，宣称=实现。**

### 2. 检索 is_semantic 撒谎 + degraded 标记无消费者 → 已真修 ✓
- `retrieval/vector.py:91-97` `is_semantic` 现为 **live property**，读 `self.embedder.model_id`，不再构造期快照。
- `embedding.py:115-116` 降级时把 `model_id` 从 `st:*` 翻成 `hashing-*`；live property 随之翻 False——降级进程不再谎报 semantic。
- `_reindex`（vector.py:180-199）降级中途重 key：lookup 用旧 key，embed 后 re-read model_id，若变了则丢弃旧 key 查询、按真实 backend 重 embed/persist——hashing 向量不会被写进 `st:*` key 投毒缓存。逻辑闭合。
- **真实消费者已接线**：`retrieval/context_pack.py:85`、`inspiration/retrieval.py:48` 均读 live `self.vector.is_semantic`（不是读已废的 `.degraded` 布尔）。即"诚实信号被真正消费"，不是挂着没人看。
- 判定：**真修，不再撒谎、不再投毒。**

---

## 二、R4 新代码压测找回归（重点）

### content/store.py（id 不变量下沉到写盘边界）—— 无 bug ✓（运行时实证）
我直接跑了 3 个用例（绕过 normalize，模拟 recognize→人审→save 路径）：
- A：合成冒号 qer id `q1:event:start` 走 `_write_quest_event_refs`（定 path、id 作 JSON 内容），**未被误拦**，save 成功。→ 合法冒号 id 没被误杀。
- B：实体 id `../escape` 走 `_write_json_dir`，**在写盘边界被 ValueError 拦**（"forbidden character(s) '.', '/'"）。→ 非 normalize 路径也兜住 traversal。
- C：实体 id `a:b`（会变文件名）也被拦。
- jsonl/aggregate 四个写法（_write_relations/_write_quest_event_refs/_write_terms/_write_style_guides）全部写**固定路径**，id/key 只作 JSON 内容、从不拼进文件名 → 不需要也不应过 traversal 校验。代码注释（store.py:140-141）的解释准确。
- 判定：**fix 完整、范围诚实，无 traversal 漏洞、无误拦。**

### multi_agent/workers.py + react.py（worker 真实错误数 / AgentStep.result）—— 无 bug ✓
- `_extract_claimed_open_errors`（workers.py:209-238）从 transcript 里成功的 `audit_project` 步骤取**结构化 `step.result["open_errors"]`**，非数 prose 子串。守卫严谨：拒 bool（int 子类）、拒负、拒非 int；无审计返回 `None`（诚实"无 audit 背书声明"），verifier 不会误读成"声称 0"。
- `AgentStep.result`（react.py:317）从 `self.registry.run()` 的 dict 结果填，**与 provider 无关**（provider 只决定 LLM 产出的 Action，tool 执行是本地的、provider-independent）；"无 Action" 分支（react.py:250）正确留 None。
- 判定：所有 provider 路径都填了（因为填充点在 tool 执行而非 LLM 调用），**无 over-claim。**

### llm/resilience.py（Failover/CircuitBreaker 的 .model 透传）—— LOW，已记录的设计权衡，**非 bug**
- 现象（即 brief 预判点）：gateway 在调用**前**用 `getattr(provider,"model")` 算缓存键 + OTEL（gateway.py:231-232）。FailoverProvider.model 返回 **primary**（resilience.py:59）。真发生 failover（命中 secondary）时，secondary 的回包会被缓存/记录在 **primary 的 model id 下**（gateway.py:265,269）。
- 诚实评估：
  1. docstring（resilience.py:52-58）**明确写**"reflects the primary — the provider actually tried first"，**没有谎称跟踪 active model**。即文案与行为一致，不算自欺。
  2. 仅在**真 failover 时**且仅 **real 模式 opt-in**（需设 `OWCOPILOT_FALLBACK_MODEL`，resilience.py:160）才触发；离线 $0 作品集默认根本不接线。可达性极窄。
  3. 替代方案（不暴露 .model）更糟——会把 primary/secondary 塌到同一 tier label。R4 的透传是净改善。
- 唯一可挑：docstring 没顺带说明"failover 后回包会落在 primary 键下"这一后果。**建议加 1 行 caveat**，但不构成 bug，不阻塞。

### content/normalize.py（印尼语 id 静默全损 → warning；显式 locale 过白名单 warning）—— 无 bug ✓
- `_validate_explicit_locale`（normalize.py:725-740）：仅当 `not _is_known_locale` 才 warn；`zh-CN` 等 region 形式通过（无误报），`zz` warn（正确），且是 warn-不-drop。
- 印尼语 `id` 列 warning（normalize.py:489-499）：仅当 **完全无任何真 locale 数据**（`not locale_values`）且保留键(id)有值时触发——有真 locale 数据时不误报，零翻译静默丢失被堵住。触发条件精确。
- 小边角：`_is_known_locale` 正则 `^[a-z]{2}(-[a-z]{2})?$` 不收 script 子标签如 `zh-Hans` → 会对 `zh-Hans` 误 warn，但 warn-不-drop、属罕见格式，与 R4 既定姿态一致。**不值得修。**

---

## 三、task 3 已知遗留项诚实评估（R4 Team-C 转交的快照模式）

`assist/sweep.py:202` 和 `assist/contradiction.py:99` 在**构造期**快照 `self.embedder = embedder if _is_semantic(embedder) else None`，并据此报 `semantic_used`（sweep.py:321）。

**诚实结论：生产接线下安全，理论边角极低危，不值得修。**
- 原因：四个生产调用点（actions.py:1416/1477/1532/1633）传的都是 `project.embedder`，而该实例在 `pipeline/project.py:60` 构造 `VectorRetriever` 时其 `_reindex()` 已调过 `embed_many` → **共享 embedder 在被 sweep/contradiction 快照前已经预热**。若 semantic 模型要降级，早在 project 构建时就降了，快照拿到的已是 `hashing-*`，`_is_semantic` 正确判 None。
- 唯一理论不安全路径：把**全新未预热的 lazy SemanticEmbedder** 直接喂给 ThemeSweepService/ContradictionDetector（仅测试/假想直连 API）。现有生产无此路径。
- 另一空 corpus 边角（vector.py:152-156 空 corpus 早返不预热）→ semantic_used 可能虚报 True，但空 corpus 下无 finding，纯属真空。
- 与 R4 判断一致（init-time feature gating，低危）。**给诚实评估：可不修。**

---

## 四、宣称 vs 实现 复核（确认 OK 的点 = 末轮背书）

- README:5-8,28-40 的产品定位（"不是替你写游戏"、"作为起点非代替你的创作"、"世界没说就拒答")——与实现一致，措辞克制不夸大。
- README "30/30 retrieval hits"：运行时 hit_rate=1.0、misses=[]，30/30 属实（gate 阈值仅 0.90，文案报的是实际达成而非阈值，公允）；docstring(acceptance.py:764)诚实标注 query "primarily verbatim"并另设 paraphrase 基准补强。
- `tests-passing` 徽章是静态徽章（非 CI 联动），属作品集常规，非欺骗。

---

## 末轮 Sign-off
我攻了：(1) README seven-gates 文案 vs 7 个 gate 实现+运行时；(2) is_semantic live property + 降级重 key 反投毒 + 真实消费者接线；(3) store.py 写盘边界 id 不变量（运行时 3 用例：合法冒号通过 / traversal 拦 / 冒号拦）；(4) worker 真实错误数 + AgentStep.result 全 provider 路径；(5) Failover/CB .model 透传；(6) normalize warning 触发条件；(7) sweep/contradiction 快照模式在生产接线下的安全性。**确认：宣称=实现，无注水，无伪闭环。** 唯一可选改进：resilience.py:48 docstring 加 1 行 caveat 说明 failover 后回包落 primary 键（trivial，非 bug）。
