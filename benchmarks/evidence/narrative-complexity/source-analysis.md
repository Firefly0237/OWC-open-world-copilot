# 叙事复杂度画像(带出处 · 三档参数集)

> 生成:2026-07-11(N2a)。目的:给 N2b 的合成基准提供**有真实出处**的结构化标尺,把"合成语料"从"堆数量"升级到"按真实叙事的结构分布采样"。
>
> 本文是 [`complexity-profiles.json`](complexity-profiles.json) 的证据与推导底稿:JSON 里每个参数名都与 `src/owcopilot/content/models.py` 的字段语义 1:1 对应(见下方映射表),N2b 直接读 `profiles[tier].params[name]` 即可,无需映射垫片。
>
> **诚实边界(硬约束):** 凡本文/JSON 出现的数字,要么可追溯到下面某个抓取确认的来源 URL,要么可追溯到已提交的 N1 台账 `benchmarks/ink_losslessness.md`。无法证实的一律**省略**或**显式标注为外推**(JSON 里同名参数带 `assumed:true`),绝不臆造。跨游戏的结构量本身是异构的(ink knot 数 / QDD 页数 / 流程图节点数不可直接比较),所以每档都在正文里写清它的**单位归一假设**。

---

## 一、参数与 `content/models.py` 字段的对应(N2b 无垫片消费)

| 画像参数 | 语义 | 对应 models.py 字段 |
|---|---|---|
| `quest_count` | 一个内容包里的任务数 | `ContentBundle.quests` 的条目数 |
| `stages_per_quest` | 每个任务的阶段数 | `len(Quest.stages)`(`QuestStage`) |
| `branch_factor` | 单个决策点的可选分支数 | `QuestLogic.branches` 在同一 `from_stage` 上的出边数 / `DialogueNode.choices` 数 |
| `convergence_rate` | 分支"回并"占比(合流 vs 终局),0..1 | `Branch.to_stage`(合流)占全部 `Branch` 的比例;`Branch.outcome` 且空 `to_stage` = 终局 |
| `logic_var_count` | 逻辑变量总数 | `len(QuestLogic.variables)`(`LogicVar`) |
| `logic_var_ratio{bool,int,enum}` | 变量类型分布 | `LogicVar.type` 的分布(`LogicVarType.BOOL/INT/ENUM`) |
| `cross_quest_ref_density` | 每任务的跨任务引用数 | `Quest.prerequisites` + `QuestLogic.unlocks` + `QuestEventReference` 之和 / 任务数 |
| `dialogue_tree_depth` | 对白树最长根→叶路径 | `DialogueTree.nodes` 沿 `choices`/`next_node` 的最大深度 |
| `dialogue_tree_width` | 对白节点的平均出选项数 | `len(DialogueNode.choices)` |
| `conditional_choice_ratio` | 带条件的选项占比,0..1 | 带非空 `condition` 的 `DialogueChoice`/`Branch` 占比 |

> 说明:`branch_factor` 与 `dialogue_tree_width` 在语义上都是"单点的出度",但落在不同对象上——前者在任务逻辑层(`QuestLogic.branches`),后者在对白层(`DialogueTree`)。二者不随游戏体量放大而放大(见下);真正随体量放大的是 `quest_count`、`cross_quest_ref_density`、`logic_var_count`。

---

## 二、逐游戏证据(每条数字内链到来源)

### A. The Intercept(支撑级锚点 · 真实可复现)

支撑级不引用外部网页,直接锚定本仓库已提交、可 `$0` 复现的 N1 台账。语料是 inkle 的 *The Intercept*(MIT),仓库在 [inkle/the-intercept](https://github.com/inkle/the-intercept),vendored 于 `tests/fixtures/ink/the_intercept/`;逐构件台账见 [`benchmarks/ink_losslessness.md`](../../ink_losslessness.md)(复现:`python scripts/run_ink_losslessness.py`)。

台账里已提交的地面真值(不重新猜):

- **30 个 knot**(`=== knot`,各成一个 event 节点)、**30 个 stitch**(子节点 `knot.stitch`)、**12 个具名 gather**(可寻址节点)。→ 映射到 `stages_per_quest`。
- **338 个 choice**(`*`/`+`),其中 40 个解析成带守卫的边;**83 个匿名 gather**是 weave 的合流点。→ 映射到 `branch_factor` 与 `convergence_rate`。
- **61 个条件守卫**(`{cond}`),59 个可翻译进逻辑 DSL。→ 映射到 `conditional_choice_ratio`:`61 / 338 ≈ 0.18`。
- **27 个声明**(VAR/CONST/LIST)。→ 映射到 `logic_var_count`。
- **4 处终局 `-> END`**(直接在 fixture 上数得,与台账同一份 committed 语料):终局极少、合流极多 → `convergence_rate` 很高。

**变量类型分布(直接在 vendored fixture 上测得,而非猜):** 台账只报"27 个声明"的聚合数,不含类型拆分,故类型比例直接对同一份 committed 语料 grep 得到——27 = 21 个运行时 `VAR` + 6 个 `CONST`(枚举成员值,在模型里落到 `LogicVar.enum_values`,不单列为变量)。21 个运行时 `VAR` 中:**17 个布尔**(`= false/true`)、**2 个整型**(`forceful`/`evasive`,`= 0` 计数器)、**2 个枚举**(`hooperClueType` ∈ {NONE,STRAIGHT,CHESS,CROSSWORD}、`smashingWindowItem` ∈ {NONE,SHOE,BUCKET})。→ `logic_var_ratio ≈ {bool 0.81, int 0.10, enum 0.10}`。这是**测得值**,可复现(`grep -E '^\s*(VAR|CONST)' TheIntercept.ink`),不打 `assumed`。

**单位归一假设(支撑级):** knot=一个场景阶段;stitch=阶段内子步(故 `stages_per_quest` 取 [15,30]:上界=30 个 knot,下界=允许琐碎 knot 合并后的保守值)。`branch_factor` 按"单个 choice-point 的平均选项数"归一:338 choice ÷(83 匿名+12 具名=95 个 gather 合流点)≈ 3.6,取 [3,4],与上层各档同一单位、可比。The Intercept 是**单个高度分叉的任务**,所以它的 per-quest 阶段数偏高、`quest_count` 偏低——这是它"整篇塞进一个任务"的形状,不是错误。

### B. The Witcher 3 / Cyberpunk 2077 QDD(章节级锚点之一)

来源:Game Rant 对 Pawel Sasko(CD Projekt Red)GDC 谈话的整理 [Inside CD Projekt Red Quest Design](https://gamerant.com/cd-projekt-red-witcher-cyberpunk-2077-quest-design-how/)(抓取确认)。要点:

- **任务设计文档(QDD)页幅随任务规模变化:** minor quest = "one page";大主线任务 = "20–30 pages";血男爵(Bloody Baron)"almost 60 pages long";凯尔莫罕之战(Battle of Kaer Morhen)"14–15 pages"却含大量角色排列。
- **结构:** QDD 用**括号+配色**标出 "here's a scene, here's the gameplay, here's the combat",以便一眼判断节奏(pacing);描述的是玩家屏幕上看到什么,而非设计者意图。
- 任务在引擎里本质是**状态机**(见 `narrative_pipeline_evidence.md` 的同一 CDPR 证据链)。

**单位归一假设(章节级 stages):** QDD 页幅是唯一硬数字,页→阶段是粗归一——按"每个配色 scene/gameplay/combat 序列 ≈ 1 个阶段、约 2 页"折算:20–30 页大任务 ≈ 10–15 个阶段,血男爵 60 页 ≈ 20–30 阶段,故 `stages_per_quest = [8,25]`。此项由 QDD 页幅+明写的归一得到,**不打** `assumed`。

### C. Detroit: Become Human(章节级样本 + 多线级整盘)

- **总章节数 = 32**,全章节名单可核:来源 [PowerPyx 全流程](https://www.powerpyx.com/detroit-become-human-full-walkthrough-all-chapters/)(抓取确认,列出 The Hostage … Battle for Detroit 全 32 章)。三主角 Connor / Kara / Markus,每章一张记录 checkpoint 与分支节点的流程图,末章 Battle for Detroit 收束所有线。
- **结局数 = 40+**,每个主角 4–6 个主要结局:来源 [RoyalCDKeys 全结局指南](https://royalcdkeys.com/blogs/news/detroit-become-human-walkthrough-all-endings-guide-2025)(抓取确认:"over 40 distinct endings";Connor/Kara/Markus 各 "4–6 major outcomes")。
- (原设计点名的 Fandom `Battle_for_Detroit` 流程图页 WebFetch 返回 402,Neoseeker 返回 403;按风险预案改用上述可达镜像,并丢弃任何镜像无法证实的精确节点数——宁可给有出处的区间,不给无法证实的点值。)

**归一:** 32 章 → 多线级 `quest_count` 下界(把一章当一个主任务单元);40+ 结局 + 每主角 4–6 出口 → 多线级 `convergence_rate` 很低(高度发散到多终局)。单章流程图(2–4 路分叉、汇入章末)→ 章节级 `branch_factor`/`convergence_rate` 的方向锚。

### D. Baldur's Gate 3(多线级整盘 · 终局广度与文本体量)

- **结局变体 = 17,000:** 来源 [PC Gamer](https://www.pcgamer.com/baldurs-gate-3-has-17000-variations-on-its-ending-a-number-that-gives-me-a-headache-just-thinking-about-it/)(标题即含 "17,000",抓取确认该数字)。归因:由 Larian 发行总监 Michael Douse 给出,导演 Swen Vincke 与主笔 Adam Smith 解释这 17,000 指结局的**反应性排列**——Smith 把叙事比作一张"big spiderweb",终点在中心、开局在外缘(归因经 GamesRadar/GameSpot/80.lv 交叉核对;PC Gamer 正文被抓取截断,故归因以交叉源为准,数字以 PC Gamer 标题为准)。
- **剧本体量 ≈ 200 万词,超《指环王》三部曲对白 3 倍:** 来源 [GamingBolt](https://gamingbolt.com/baldurs-gate-3-script-is-roughly-2-million-words-long-new-origin-character-revealed)(抓取确认:"roughly two million words long";"over three times the dialogue of all three Lord of the Rings novels combined")。交叉核对:Guinness World Records 记录精确值 **2,503,837 词**(其中 2,121,425 为角色对白),并列出 **174 小时**过场动画(约为《权游》全剧两倍);单次通关官方时长 75–100 小时。

**从 17,000 推逻辑变量量级(显式外推):** 17,000 个结局排列至少需要 `ceil(log2(17000)) = 15` 个相互独立的结局因子;整盘游戏跨全程追踪的状态远多于结局因子,故多线级 `logic_var_count` 取 [100, 2000] —— 下界的推理锚(≥15 个结局因子)有出处,整盘几百到上千的带宽是**外推**,JSON 里 `logic_var_count.assumed = true`。

---

## 三、三档画像(逐参数说明"由哪个源、怎么推出来")

三档不是可直接互比的同尺度放大,而是三种**形状**:支撑级=一个深分叉的小任务;章节级=若干相连的中等任务;多线级=几十到上百任务+深跨任务引用网+极宽终局。`stages_per_quest`、`branch_factor`、`dialogue_tree_width` 在三档间刻意**重叠**(单点出度/单任务内部规模不随体量放大);真正拉开档次的是 `quest_count`、`cross_quest_ref_density`、`logic_var_count`、以及 `convergence_rate`(支撑高→多线低)。

### support(支撑级)—— 锚:The Intercept N1 台账

| 参数 | 值 | 由来 |
|---|---|---|
| quest_count | [1, 3] | The Intercept 是**一篇独立故事**→下界 1 有出处;上界 3 是"一个支撑级世界可容纳几篇小品"的生成余量 |
| stages_per_quest | [15, 30] | 台账 30 knot → 上界 30;下界为琐碎 knot 合并的保守值 |
| branch_factor | [3, 4] | 338 choice ÷ 95 个 gather 合流点 ≈ 3.6(单点选项数归一) |
| convergence_rate | [0.80, 0.95] | 仅 4 处 `-> END` 终局 vs 338 choice / 83 匿名 gather 合流 → 极高合流 |
| logic_var_count | [21, 27] | 21 运行时 VAR ~ 27(含 6 CONST 枚举值),台账/fixture 实测 |
| logic_var_ratio | bool[0.75,0.85] int[0.05,0.15] enum[0.05,0.15] | 21 个 VAR 实测 17/2/2(≈0.81/0.10/0.10),对同一 committed 语料 grep 可复现 |
| cross_quest_ref_density | [0.0, 0.5] | The Intercept 独立成篇、无前置/解锁 → 下界 0 有出处 |
| dialogue_tree_depth | [6, 15] | **外推**(`assumed`):The Intercept 对白内嵌于 weave、无独立对白树,深度按场景链粗估 |
| dialogue_tree_width | [2, 4] | 与 branch_factor 同一 choice-point 出度证据 |
| conditional_choice_ratio | [0.15, 0.20] | 61 守卫 / 338 choice ≈ 0.18 |

支撑级除 `dialogue_tree_depth` 外全部硬锚定——这正是它作为"真实、可测、$0"基准的意义。

### chapter(章节级)—— 锚:Witcher QDD 页幅 + 单个 Detroit 章流程图

| 参数 | 值 | 由来 |
|---|---|---|
| quest_count | [3, 12] | **外推**:一章/大任务线聚合数个任务(血男爵是多段任务线);方向锚自 CDPR"一个设计师拥有一整块内容" |
| stages_per_quest | [8, 25] | QDD 20–30 页大任务、血男爵 60 页,按"1 配色序列≈1 阶段≈2 页"折算(不 `assumed`) |
| branch_factor | [2, 4] | **外推**:Detroit 单章 checkpoint 2–4 路、Witcher 对白轮 2–4 选项 |
| convergence_rate | [0.40, 0.70] | **外推**:Detroit "每条分支都收束到 Battle for Detroit",但保留变体出口 → 中等合流 |
| logic_var_count | [20, 80] | **外推**:按 The Intercept 21 变量/小品 × 数个任务折算 |
| logic_var_ratio | bool[0.55,0.75] int[0.10,0.25] enum[0.10,0.25] | **外推**:沿 The Intercept 布尔主导的形状,给关系/线索态更多枚举 |
| cross_quest_ref_density | [0.5, 3.0] | **外推**:章内任务经前置相互串联 |
| dialogue_tree_depth | [5, 12] | **外推**:Witcher/Detroit 分支对白深度粗估 |
| dialogue_tree_width | [2, 4] | **外推**:对白轮典型 2–4 选项 |
| conditional_choice_ratio | [0.20, 0.40] | **外推**:较支撑级(0.18)上调,章级更多状态门控 |

### multiline(多线级)—— 锚:Detroit 整盘流程图总量 + BG3 终局/文本

| 参数 | 值 | 由来 |
|---|---|---|
| quest_count | [30, 150] | 下界 32 有出处(Detroit 32 章);上界 150 为 BG3 量级(较软) |
| stages_per_quest | [6, 25] | 沿用 Witcher QDD 的 per-quest 规模(单任务内部规模不随整盘放大) |
| branch_factor | [2, 5] | **外推**:对白轮 + 属性检定选项略宽 |
| convergence_rate | [0.10, 0.40] | **外推**(方向强锚):BG3 17,000 终局、Detroit 40+ 终局 → 极低合流/高发散 |
| logic_var_count | [100, 2000] | **外推**:17,000≈2^14 → ≥15 结局因子(下界推理有出处);整盘几百到上千为外推 |
| logic_var_ratio | bool[0.45,0.65] int[0.15,0.30] enum[0.15,0.30] | **外推**:布尔仍主导,阵营/任务态/等级带来更多枚举 |
| cross_quest_ref_density | [2.0, 10.0] | **外推**(方向强锚):BG3 "spiderweb" 反应性、Detroit 全局变量门控后续节点 |
| dialogue_tree_depth | [8, 25] | **外推**:BG3 同伴长线对白 |
| dialogue_tree_width | [2, 6] | **外推**:属性检定门控使选项更宽 |
| conditional_choice_ratio | [0.30, 0.60] | **外推**(方向强锚):BG3 大量按出身/属性/历史门控的对白 |

---

## 四、来源清单(与 JSON `sources` 一一对应)

| id | 类型 | URL |
|---|---|---|
| intercept_n1 | corpus | https://github.com/inkle/the-intercept |
| witcher_qdd | gdc-talk-summary | https://gamerant.com/cd-projekt-red-witcher-cyberpunk-2077-quest-design-how/ |
| detroit_chapters | walkthrough | https://www.powerpyx.com/detroit-become-human-full-walkthrough-all-chapters/ |
| detroit_endings | walkthrough | https://royalcdkeys.com/blogs/news/detroit-become-human-walkthrough-all-endings-guide-2025 |
| bg3_endings | news | https://www.pcgamer.com/baldurs-gate-3-has-17000-variations-on-its-ending-a-number-that-gives-me-a-headache-just-thinking-about-it/ |
| bg3_wordcount | news | https://gamingbolt.com/baldurs-gate-3-script-is-roughly-2-million-words-long-new-origin-character-revealed |

**抓取备注:** Fandom(HTTP 402)与 PC Gamer / GamesRadar 正文(截断)在设计期即被 WebFetch 挡下;Detroit 数据改由 PowerPyx/RoyalCDKeys 镜像证实,BG3 词数由 GamingBolt 证实、经 Guinness/GamesRadar 交叉核对,BG3 17,000 终局由 PC Gamer 标题证实、归因经 GamesRadar/GameSpot 交叉核对。凡镜像也无法证实的精确点值一律丢弃,只保留有出处的区间。
