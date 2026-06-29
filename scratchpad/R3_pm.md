# R3 复审 · 身份：竞争对手资深项目经理（挑刺/务实/戳穿注水）

被测：OWCopilot @ `feature/agent-pipeline-enhancements`，HEAD `8cfd19b`（已过 R1/R2 两轮修复）。
立场：不夸，只找「新的、具体的、可复现的」自欺/注水/伪闭环。已知结构性战略软肋（CLI 门槛/无协作/无插件/无 SaaS）不重复报。

---

## 结论一句话
没找到"演示能跑真实就塌"的硬伪闭环——核心管线（确定性审计、impact 图遍历、MCTS/ToT、压缩 harness）是**真实现且诚实标注**，多数"看起来厉害"的点经我实测站得住。但发现 **3 个"指标口径放水/命名过度承诺"的真问题**，都可复现，根因是**评测的分母/语义被悄悄收窄，而对外标题没披露这层收窄**——这正好踩在用户最在意的"不静默降级/不自欺"红线的擦边带。

---

## 真问题（按严重度）

### 【中】问题 1：QA "grounded-or-refuse" 名不副实——实体存在但事实不在 canon 时不拒答，反而高置信编答案
- **根因**：`verify_qa_answer`（`src/owcopilot/qa/verify.py:10-49`）只校验三件事：非拒答必须有 citation、citation 必须在 context pack 内、mentioned_entities 可解析。它**从不校验"被引用的内容是否真的支撑了问题的答案"**（无 entailment/NLI）。所以只要模型/离线 provider 引用了一个**被检索到的真实 ref**，哪怕该 ref 文本完全没回答问题，也判 `grounded=True`。
- **证据（可复现）**：`scratchpad/qa_probe.py` → `scratchpad/qa_probe.txt`。对 acceptance 世界提问"实体在 canon、但具体事实不在 canon"的问题：
  - `铁卫军团的军歌歌词` → refused=False, 6 citations, conf=0.75
  - `沈清河喜欢吃什么` / `沈清河的生日是哪天` → refused=False, 3 citations, conf=0.75
  - `雾脊山道有多少条河流` → refused=False, 7 citations, conf=0.75
  - `铁卫军团的总部在哪` → refused=False, 6 citations, conf=0.75
  - 仅"**完全不存在于世界**"的问题才拒答（`龙王是谁`/`谁偷走了月亮`/`铁卫军团的叛徒是谁`）。
- **gate 为什么没抓到**：`acceptance.py:946-961` 的 `qa_grounded_or_refuse` 检查，unanswerable 只放了 `龙王是谁`/`谁偷走了月亮` 这种**世界外**问题。它**从不测**"实体在、事实不在"这一最常见也最危险的幻觉场景。所以 gate 绿 ≠ 拒答能力真达标。
- **真 bug 还是设计选择**：**命名过度承诺**。机制本质是"cited-a-retrieved-ref-or-refuse"（引用存在性），不是"answer-is-supported-by-citation-or-refuse"（引用支撑性）。
- **公平话**：① 这是离线 `OfflineQAProvider`（非 LLM 桩）放大的，但**校验层 `verify.py` 对真 LLM 路径同样不做支撑性检查**，所以真模型乱引一个真 ref 也照样过。② 全世界人审/只读，错误 QA 答案不会污染 canon，所以不是"灾难级"。③ 求职展示角度：这是 RAG 系统的经典弱点（无 entailment 校验），**诚实标注它**比假装"grounded"更值钱。建议把 gate 命名/文档改为"citation-existence grounding"，并补一条"in-world entity / out-of-canon fact → 应拒答"的反例 gate。

### 【低-中】问题 2：`detection_rate=1.0` 的分母被悄悄收窄到 20/29 条规则，且未披露
- **根因**：`seed_errors`（`acceptance.py:421-538`）播种的 25 个错误只覆盖 **20 种** rule_code。注册表实际有 **29 种**（`build_default_rule_registry().codes()`）。9 条规则在 acceptance 世界里**零覆盖**：`PROMPT_INJECTION`、`QUEST_LOGIC`、`QUEST_GLOBAL_UNREACHABLE`、`QUEST_MISSING_OBJECTIVE`、`UNREVIEWED_AI_CONTENT`、4× `DIALOGUE_TREE_*`（broken_link / unknown_speaker / unreachable_node / undefined_var）。
- **证据（可复现）**：见我跑的 registry-vs-seeded 对比（29 total，20 seeded，列出 9 个未覆盖）。
- **为什么算口径放水**：acceptance.py 模块 docstring（`:1-8`）说"seeds 25 classified errors and measures rule detection"，报告输出 `detection_rate: 1.0`。一个善意读者会把"detection_rate=1.0"理解成"规则检测全面验证通过"。实际上**安全相关的 `PROMPT_INJECTION`、整套对话树规则都不在这个指标里**。这份文件别处的诚实标注极其细致（F1=1.0 by construction、样本量不够 Wilson CI、verbatim 查询只测 BM25……），唯独这条指标的"分母 scope"没标注，形成对比下的盲点。
- **公平话**：这 9 条规则**都有专门单测**（`PROMPT_INJECTION` 6 个测试文件、`UNREVIEWED_AI_CONTENT` 16 个、其余 4-9 个），所以**不是未测代码**，是"acceptance gate 覆盖范围未披露"。严重度因此压到低-中。建议：在 metrics 里加 `rules_covered: 20/29` 或在 docstring 注明"detection_rate 仅覆盖 reference/graph/lore/region/localization 五类规则，dialogue-tree 与 injection 由专项单测覆盖"。

### 【低】问题 3：auto 模式 embedder 降级有 warning 但产物无机读降级标记
- **根因**：`SemanticEmbedder.embed_many`（`retrieval/embedding.py:90-106`）首次加载失败时降级到 `HashingEmbedder`，**有 `logger.warning`**（符合"不静默降级"底线的最低档）。但生成的 `ContextPack` / QA 答案**不携带任何机读字段**标明"本次结果走的是 BM25-only 降级路径"。
- **为什么提**：用户红线是"任何降级必须显式标注"。日志 warning 满足了"对运维显式"，但**对下游消费者/UI/调用方不可见**——读到一个 context pack 的人无法判断它是否真用了语义检索。这是可观测性缺口，不是静默降级的硬违反（warning 在）。
- **真 bug 还是设计选择**：边界增强项。`mode=="semantic"` 显式 opt-in 时是 fail-loud（`embedding.py:155-159`），这点做得对。仅 auto 模式的产物缺降级溯源标记。建议：降级时在 ContextPack/QAAnswer 上打 `retrieval_degraded: true` 或类似 telemetry 标记。

---

## 我验证过、确认真实可信的点（验收正向信号）

- **MCTS 修复搜索是真 MCTS**（`patches/search.py`）：UCB1 选择 / 扩展 / 随机 rollout / 回传齐全，奖励=确定性审计（免费），seeded 可复现。**没有注水**。
- **"MCTS 击败 greedy"诚实标注、未夸大**（`evaluation/repair_bench.py:1-12`）：模块 docstring **主动承认**"在确定性 fixer 上修复相互独立，greedy 已达最优，MCTS 只在候选相互作用时才赢"，且 `_trap_world`/`_trap_candidate_provider` 被明确标为"controlled interacting world"。这是**反自欺的范例**，不是制造的胜利。
- **ToT 是真 beam search**（`worldgen/tot.py`）：`tree_of_thoughts` 是通用 propose→evaluate→prune 原语；premise 应用在 steps=1 用，但原语本身支持多层；LLM evaluator 有确定性 floor + 不可解析时降级到确定性分（honest-failure）。诚实标注 deterministic score 会饱和、所以才上 LLM judge。
- **ReAct loop 真把工具结果回喂模型**（`agent/react.py` + `agent/offline.py:1-9`）：offline provider 的 Final Answer 从 transcript 里 scrape `open_errors` 数字，过测证明 loop 真在反馈而非 replay 脚本。
- **context compressor 是真 LLM 压缩**（`agent/context_compressor.py`）：append-only 源不变、checkpoint 永不进压缩批、压缩失败 fail-safe 回退、token 账目机读返回。所有降级都有日志/标记。
- **F1=1.0 / tool-selection gate 诚实标注**（`acceptance.py:1010-1132` + `agent/offline.py:78-104`）：明确写"OfflineGoalAwareReActProvider 被设计成精确返回 gold 序列，offline F1=1.0 是 by construction，不代表真 LLM 工具选择精度"，并打了机读标签 `is_sanity_gate=True`。不是放水，是把放水点**主动标红**。
- **impact_recall_100 是正确的 recall gate**（`acceptance.py:872-903`）：实测 `delete fac_iron` 返回 24 MUST_CHANGE + 80 SUGGEST_CHECK（图遍历很全），gate 的 expected 集是其**子集**（`{npc_r1_a,npc_r2_a}`⊆24），所以是"never-miss-known-critical"的召回门，不是精度自夸。写法正确。
- **embedder semantic 模式 fail-loud**：显式 opt-in 时加载失败直接抛，不偷偷降级（符合红线）。

---

## 给用户的一句话总结
没有发现"demo 能跑真实就塌"的硬伪闭环，核心 agent 范式（ReAct/MCTS/ToT/压缩）都是真实现且**诚实标注是这个项目最强的护城河**；真问题集中在 3 处"评测口径/命名过度承诺"——最值得修的是 QA 的 grounded-or-refuse 实为"引用存在性"而非"引用支撑性"（实体在、事实不在时会高置信编答案，gate 从不测这个场景），以及 detection_rate=1.0 的分母悄悄只覆盖 20/29 条规则（含安全相关的 PROMPT_INJECTION 未进 gate，虽有单测兜底）——都属"披露不足"而非"造假"，补一句诚实标注即可闭合。
