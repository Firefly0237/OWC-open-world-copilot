# 架构导览（自用 · 复习项目结构）

> 目的：几个月后重开这个项目，先读这份，快速回到设计心智。
> 代码是按**领域分包**（每个包一份自己的 `models.py`/`offline.py` 等，是刻意的，不是混乱）。
> 下面把 `src/owcopilot/` 的包按**逻辑分层**归类——层只是心智模型，代码仍平铺在 `owcopilot/` 下。

## 产品定位

**游戏策划辅助工作 agent 平台。** 产出物 = 策划工作材料（设定库/审查报告/笔记/配表/本地化包）+
**beta** 引擎导出（UE/Unity：功能与 UE 5.7 真机验证为真——见下「交付」一层的 `exporters/`/
`integrations/`——但字段命名/目录结构/导出流程未经真实项目校准，不等于生产可用）。降级为 beta
不删证据：UE 5.7.2 真机零差异回归测试原样保留（`tests/integrations/test_ue_live_import.py`）。

## 一句话

local profile 以文件为正典真源；N1.2 起 team canonical 以 PostgreSQL 不可变 revision 为真源 →
建索引与图 → 确定性审计当"世界的 linter"（规则覆盖范围内报的都是真错，不报≠没错）→
agent 与生成都绕这套规则转 → 人审入库 → 交付数据与本地化（引擎导出为 beta）。当前 team
只迁入 canonical kernel，未迁端点 fail closed，不与 local 双写。

## 分层与各包职责

### 接口层（外部怎么调）
| 包 | 职责 |
|---|---|
| `service/` | FastAPI REST + SSE，前端与外部集成的主入口 |
| `cli/` | 命令行入口，所有子命令；JSON 输出，可进 CI |
| `mcp_server/` | 工具实现体（`tools.py`）+ MCP 传输层；`skills/` 注册表延迟绑定同一批工具实现（方向：skills 镜像 mcp_server 的实现，不是反过来） |
| `app/` | Web 工作台的动作层（人工编辑 + 审阅决策 + 引擎往返 **beta**） |
| `pipeline/` | 工作流枢纽（harness/export_gate/ingest 等），被 app/cli/mcp_server/service/evaluation 五方复用 |
| `platform/` | 多租户控制面（auth/tenancy/store），service 依赖 |
| `integrations/` | 引擎远程客户端（UE Remote Control / Remote Execution 薄壳，**beta**：真机连通性未独立验证，见下 `exporters/` 的真机验证范围） |

### 内容领域（世界是什么）
| 包 | 职责 |
|---|---|
| `content/` | 内容模型（Entity/Quest/Relation…）、文件存取、版本 overlay、`engine_coerce`(引擎行→v2 Quest) |
| `audit/` | **35 条确定性一致性审计**——规则覆盖范围内的确定性判据（linter 语义），贯穿所有 agent 范式 |
| `graph/` | 内容图 + 社区检测（GraphRAG-style 用） |
| `logic/` | QuestLogic 状态机（阶段/条件/变量） |
| `impact/` | 改动影响面分析（纯图遍历，零成本） |
| `readiness/` | 就绪度评分（完整度，非正确性） |
| `search/` · `templates/` · `localization/` | 字面搜索 · 内容模板 · 本地化导出 |
| `exporters/` · `compliance/` · `collab/` · `inspiration/` | 数据包导出（引擎表导出 **beta**：UE 5.7.2 真机验证覆盖 11 张表里的 3 张 + 1 张可选表，Unity 侧代码从未编译执行，字段命名/目录未经真实项目校准） · 版号合规 · 协作 · 灵感库（含 `leakage.py`：草稿 × 被引素材的逐字重合/专名照搬确定性检测，**不是上面 35 条规则之一**，结论作为 `reference_report` 的字段交给人审，不产出 Issue、不拦截；含 `grounding.py`：草稿链路"吃素材"的统一注入形态——检索用途 `drafting` / prompt 独立围栏段 / `inspiration_refs` metadata / 溯源+泄漏重算，五条链路一处定义，实现入口为 `src/owcopilot/inspiration/grounding.py`） |

### 生成与提炼（怎么造/怎么进）
| 包 | 职责 |
|---|---|
| `worldgen/` | 分阶段世界生成（premise→区域→实体→任务）+ critique-refine + ToT |
| `assist/` | 单件生成的助手环：critic/refine(Reflexion)/calibration(Wilson)/lessons/dpo_export/characters/dialogue_trees… |
| `extraction/` | 从文稿提炼结构化内容 + 蕴含校验 |
| `recognize/` | 关系/实体识别 |

### 智能体层（agent 怎么工作）
| 包 | 职责 |
|---|---|
| `agent/` | 有界 ReAct 循环（文本 + 原生 function-calling，默认原生），上下文压缩，OTEL span |
| `skills/` | 技能/工具注册表（类型化参数校验 + 副作用标注），MCP 与之镜像 |
| `multi_agent/` | orchestrator-worker-verifier 群 + SQLite blackboard；verifier 独立重跑审计裁决 |
| `patches/` | MCTS 修复规划（UCB1，审计错误数=可验证奖励）；apply/rollback/shadow 校验 |
| `prompts/` | prompt 注册表（用途/task/输出契约/版本），防漂移测试保证新 prompt 必登记 |

### 检索层（RAG）
| 包 | 职责 |
|---|---|
| `retrieval/` | 混合检索(BM25+dense+graph+GraphRAG-style)+RRF+cross-encoder；4 向量后端(fp32/int8/usearch HNSW)；`policy`按规模分层；`agentic`有界多跳循环 |
| `qa/` | 世界问答服务（带引用/拒答）+ faithfulness 蕴含判据 + 社区报告 |

### 评测层
| 包 | 职责 |
|---|---|
| `evaluation/` | acceptance/golden/repair 基准 + 五维度 agent scorecard；评测即 CI |

### 基础设施
| 包 | 职责 |
|---|---|
| `llm/` | LLM 网关（缓存/路由/重试/成本遥测/OTEL 桥/原生 tools） |
| `storage/` | SQLite 存储层（FTS5/vec0/WAL/scope 分片） |
| `teamdb/` | N1.2 PostgreSQL team canonical kernel：连接池/事务内 RLS scope、不可变 revision repository、legacy archive 与 genesis bootstrap；不含 pgvector/outbox/backup |
| `trust/` | 溯源(provenance) + 安全(注入扫描/路径守卫) |
| `telemetry/` · `util/` · `fakes/` · `assets/` | 成本遥测 · 通用工具 · 测试假件 · 内嵌 UI 资产 |

## 核心数据流

```
已有素材/新想法/引擎改动
      ↓ ingest / worldgen / extraction
local: content（文件真源）
team: PostgreSQL immutable revision（N1.2 仅 canonical kernel）
      ↓ 可重建 graph / retrieval projection
      ↓
audit（35 条确定性规则）——世界的 linter，也是 agent/MCTS/verifier 的 ground-truth 奖励
      ↓
agent 诊断 / 检索问答 / 修复提议（都只读或提议，不直接写库）
      ↓ review_queue（人审）= 唯一写 canon 的路径
入库 → calibration/lessons 回喂 prompt（数据闭环）
      ↓
exporters（数据包）+ localization（CSV/XLIFF）+ 引擎往返
```

## 三条贯穿设计主线（记住这三条就抓住了全局）

1. **确定性审计 = 已编码约束的 ground truth**：不靠模型自评，靠 35 条可复算规则（linter 语义：
   覆盖范围内的确定性判据，不等于全部叙事质量）。它同时是 MCTS 的奖励、
   多智能体 verifier 的裁决、生成-验证闭环的验证器——这是让 LLM 范式可验证的关键。
2. **离线确定性替身 = 可复现测试**：`fakes/` + 各包 `offline.py` 让整套测试不依赖真实模型跑通，
   证明"接线/逻辑正确"（真实质量另看 benchmarks/ 的 live 实跑留档）。
3. **人审 = 唯一写入**：agent 只能读/提议，写 canon 必须过 `assist/review_queue.py`。

## 目录约定

- `src/owcopilot/` 源码 · `tests/` 按领域分组的测试（agent/core/domain/evaluation/intake/integrations/mcp/platform/retrieval）· `apps/web/` Vue 工作台
- `benchmarks/evaluation/` 冻结评测数据与报告 · `benchmarks/formal/` 正式评测定义 · `benchmarks/evidence/` 当前系统结果与复杂度依据 · `scripts/` 运维和可复算入口
- `infra/team/` 团队部署产物 · `infra/migrations/` Alembic 迁移 · `content/` 测试用金样世界 · `demo/` 可运行演示材料
- `integrations/unreal/OWCopilotBridge/` UE 内容型插件（随 UE 工程发布，不进 wheel）——**beta**
- `integrations/unity/com.owcopilot.content-import/` Unity UPM 包 + `integrations/unity/offengine-tests/` 离线 C# 差分测试工程——**beta**（C# 从未编译执行）

### 已知取舍（诚实边界）
- **模型调用有两条路，agent 路不过 `LLMGateway`**（N7.2 显式决策，非迁移残留）。文本路（创世/问答/
  提炼/辅助）经网关，拿它的客户端缓存与 task→tier 路由；agent 路在装配点直接建 LangChain
  `BaseChatModel`（`app/actions/agent.py:46` `build_agent_chat_model`，CLI 与 REST 作业共用 →
  `llm/chat_model.py:329`
  `ChatModelProvider.chat_model` → `:75` `build_chat_model`）交给 `create_agent`，因此**无网关缓存、
  无网关路由**。理由（判据都可现场核）：①网关只有 `complete(system, user)` 一个文本入口且按整段
  prompt 做键（`llm/gateway.py:223`、`llm/cache.py:29` `CacheKey`），而 agent 每轮的 prompt 是不断
  变长、含 run 专属工具观测的整条 transcript，第 1 步之后精确命中率趋零；且一轮请求带 tools、回包
  带结构化 `tool_calls`，网关根本没有工具调用入口，"复用网关缓存"等于另建一套缓存面。②网关按
  task 名逐次选 tier，而 `create_agent` 整轮绑定一个模型，tier 在建模型时就定了；何况全仓 7 个
  `LLMGateway(...)` 装配点（`cli/main.py:1192`、`app/actions/_shared.py:126`、`service/api.py:128`、
  `service/routers/retrieval.py:131`、`mcp_server/tools.py:233`、`evaluation/acceptance.py:974`、
  `evaluation/golden.py:87`）无一例外只注册 `{"cheap": provider}` 并把所有 task 映到它，路由本就是
  单档直通——agent 路"少掉"的那个 tier 现实里并不存在。**代价**：换供应商要动两处
  （网关的 `providers` 与 `build_chat_model`）；agent 若要跨 run 复用 prompt 得在 LangChain 侧另接
  `BaseCache`。**不受影响**：成本/时延遥测、重试、failover 与熔断都在（见 `agent/react.py:93-133`
  的决策段）。
- 4 组包级依赖环（content⇄audit、graph⇄audit、graph⇄impact、audit→logic→assist→audit）由
  `__init__` 不导出/函数内延迟导入刻意切断，245 个模块导入烟测零失败；根因是三处职责错层
  （graph 内的 *_view 视图构建器实为 app 层、logic/draft.py 的 AI 起草实为 assist 层、
  content/ingest.py 对 audit 规则的上行引用），列为后续结构迁移项而非当前风险。

## 自研决策表（标准件 | 为何不用 | 我们的形态）

> 口径：**默认用标准件**，自研必须能说清"标准件在这件事上具体缺什么"。下表的版本事实按当前
> pin 的 `langchain 1.3.14` / `langgraph` 实测（签名、钩子、默认值都是跑出来的，不是凭印象），
> 与对应模块的注释同源——改一处必须两处一起改。凡官方件存在的，先写清"它有"，再写为何仍自研。

| 标准件 | 为何不用（实测理由） | 我们的形态 |
|---|---|---|
| `SummarizationMiddleware` | 它挂 `before_model`，返回 `RemoveMessage(REMOVE_ALL_MESSAGES)` + 摘要 + 保留段——**改写 graph state**，被压掉的原文在 checkpoint 里不复存在 | `ContextCompressionMiddleware`（`agent/middleware.py`）：`wrap_model_call` 读时投影 `request.override(messages=view)`，state 永不动；压缩批的单位是完整一轮（AIMessage + 它的 ToolMessages），不拆 assistant/tool 对 |
| `ContextEditingMiddleware` | **它同样是读时**（`wrap_model_call` + override，不改 state），这点与我们一致，所以不是"没看见官方件"；差在策略：默认 `ClearToolUsesEdit` 按总 token 触发、把较旧的 tool 结果整段换成占位符（keep 最近 3 条、无取回路径），既不是摘要，也管不了"单条观测过大" | 拆成两件：整轮摘要=`ContextCompressionMiddleware`；单条观测超限=`ObservationOverflowMiddleware` 的**可恢复**截断（marker + run 级 overflow store + `read_observation` 技能取回原文） |
| `ToolErrorMiddleware` | 1.3.14 **确有此件**（`on_error(exc, request)` 返回内容则转 `ToolMessage(status="error")`，返回 `None` 则继续抛）。不用它是**合并职责**的取舍：①回调只能返回消息*内容*，写不了 `response_metadata`，而 `owc_error_code` 要留给控制台/遥测这些旁路消费者读；②同一个 `wrap_tool_call` 里还有四件没有官方件的事（可恢复截断、run 级串行锁、`execute_tool` span、按步时延/成本归因），它们本来就必须共享同一个 step_idx 与遥测快照窗口 | `ObservationOverflowMiddleware`（`agent/middleware.py`）：五件事一个钩子，错误出口与成功出口同构（统一信封 + 机器可读 `error_code`） |
| LanceDB 一类嵌入式向量库 | 向量要与 FTS5 全文行、内容/审计行同库同连接：`content_fts`/`reference_fts` 建在 `storage/sqlite.py`，vec0 虚拟表由 `SqliteVecBackend(self.conn, …)` 建在**同一个连接**上，混合检索（BM25+dense+图）才能一次重建、不出现两份存储对不上的窗口。引第二套存储会破坏"一个目录=一个项目、离线可复现"——那是确定性审计的前提 | `retrieval/vector_backend.py`：一个 `VectorSearchBackend` 接口挂三档——sqlite-vec fp32（默认、无损）/ int8 粗排 + fp32 精排 / usearch HNSW 磁盘 ANN（索引是库旁一个 `.usearch` 文件 + 库内权威 sidecar，失步可自愈重建），按语料规模选档。ANN 不是没做，是做在同一份存储里 |
| LangGraph `StateGraph` state（把消息塞进通道） | 通道是本轮运行的快照：没有 append-only、可归属（from→to）的协作轨迹，也没有"同一 assign_id 只能被执行一次"的幂等闸；把 payload/transcript/verdict 塞进通道还会让 checkpoint 随内容长 | **两者并存，不是替代**：图管调度与顺序（`multi_agent/session.py` 的 `_OrchestrationState` 只带 goal / 标志位 / message id 与末端 report，`operator.add` reducer 按 Send 顺序合并），SQLite 黑板管内容与业务通道（`multi_agent/blackboard.py`：只 INSERT + `claim_assignment` 的条件 UPDATE 幂等闸，消息流即 CLI 回显的协作证据）。**诚实边界**：`db_path` 默认 `:memory:`，生产 CLI 未传库路径 ⇒ 黑板不跨进程持久化，跨进程续跑本就不承诺 |
| `ModelCallLimitMiddleware` + 收尾 | 步数上限与跳转**直接用官方件**（`exit_behavior="end"`）；缺的只有"耗尽后再花一次调用、从已有轨迹给个诚实答案"这一步 | `BudgetFinishMiddleware`（`agent/middleware.py`）挂 `after_agent` 结算 `owc_stop_reason` 三态，并在 generate 模式补那一次调用。**出处更正**：旧注释记成"对标 `AgentExecutor.early_stopping_method='generate'`"，但该类 langchain 1.x 已不再提供（1.3.14 里 `from langchain.agents import AgentExecutor` 直接 ImportError）——它只是这个选项的命名与语义出处，不是现行 API |
| `create_agent` 对"未绑定工具名"的默认处理 | 1.3.14 实测：`request.tool` 为空时宿主自己回一条 `status="error"` 的 `ToolMessage`（正文是通用的 "... is not a valid tool, try one of [...]"），图正常继续、模型能自纠（`tests/agent/test_skills_langchain_tools.py:122` 就是这条默认行为的实证）。够用，但它落在我们的统一错误信封之外（没有 `error_code` / `owc_error_code` 给控制台与遥测读），而且"技能根本不存在"与"技能存在但越权"这两种截然不同的原因，在它嘴里是同一句话 | `_RegistryFallbackMiddleware`（`agent/react.py:225`）把这类调用压回构造时那张 registry，让注册表用自己的措辞回答（`unknown skill '<name>'` → NOT_FOUND / `not in this agent's allowed tool set` → NOT_FOUND），再由 `ObservationOverflowMiddleware` 包成统一信封。**N7.1 裁决＝保留但收权**（它毕竟是第二条执行面）：一个*已注册*的技能只有在 ①registry 本身就是 `ScopedSkillRegistry`、②该技能 `side_effect=READ_ONLY` 时才在这里真执行，否则一律 `SkillConflictError`（CONFLICT）拒绝且 handler 不跑——堵死"未 scope 的 registry ＋ 更窄的 `allowed_skills`"这一形态（W1-1 在 API 面的残留）。两侧各钉一条测试：`tests/agent/test_agent_react.py:353` 越权被拒（并断言 handler 从未被调用）、`:373` 作用域内只读技能仍可分派 |
| `create_agent(store=BaseStore)` | 官方形参确实在签名里（1.3.14），节点侧从 `Runtime.store` 取；但 BaseStore 的语义面是 namespace+key 的 KV：`put/get/delete/search/list_namespaces`，`search` 只按单一 score 排（条目的 `created_at/updated_at` 不参与排序），TTL 是可选能力（`supports_ttl` 默认 `False`）且语义是到期删除 | `agent/episodic.py` 在其语义**之外**另建：三因子排序（relevance×recency×importance）、`retention=recency×importance` 的容量软删除、墓碑 + 逐条 restore、只比同一 `model_id` 嵌入空间。补充不是替代；真要接官方宿主，正解是写一个 BaseStore 适配器把 `put/get/search` 映射到这些表（详见该模块头） |

## agent harness 索引：关注点 → 仓库位置

> 目的：agent 相关代码分布在多个包中。下表按常见 Agent harness 关注点
> （不是本项目的包名）列出每个关注点落在哪个文件、哪一行、对应哪个符号——每行都是实测存在的
> file:line，不是从记忆写的。改一处代码，先改这里对应的行号。

| 关注点 | 仓库位置 | 说明 |
|---|---|---|
| runtime loop | `agent/react.py:611` `create_agent(...)` | 有界 ReAct 循环的宿主调用点；`middleware=` 挂下面「扩展点」整条列表 |
| 产品入口（装配） | `app/actions/agent.py:115` `run_agent_diagnose_action` | 单 agent 诊断 run 的唯一装配路径（checkpoint / thread 策略 / scoped registry / 记忆召回 / HITL 门 / 留档 / 计费），两个前端共用：CLI `cli/main.py:_cmd_agent`（只剩参数解析与 stderr 警告）、REST 异步作业 `service/routers/jobs.py:62` 的第 7 种 kind `agent_diagnose`（前端页 `apps/web/src/pages/AgentPage.vue`，含审批门的暂停/批准往返） |
| 扩展点（middleware） | `agent/middleware.py:184` `ObservationOverflowMiddleware`、`:308` `ContextCompressionMiddleware`、`:402` `MemoryInjectionMiddleware`、`:439` `BudgetFinishMiddleware`、`:518` `TelemetrySpanMiddleware` | 五件官方 `AgentMiddleware` 钩子实现，装配顺序见 `react.py:573-611` |
| checkpoint | `storage/checkpointer.py:102` `class CheckpointStore` | 包一个 `SqliteSaver`（独立 `checkpoints.sqlite`）+ TTL/容量清理（`sweep`）+ `resolve_thread_id`（`:68`），交给 `create_agent(checkpointer=...)` |
| 上下文压缩 | `agent/middleware.py:308` `ContextCompressionMiddleware`（整轮摘要）、`:184` `ObservationOverflowMiddleware`（单条观测截断） | 都是 `wrap_model_call`/`wrap_tool_call` 读时投影，state 不落盘变形（对比见上「自研决策表」两行） |
| 工具与权限 | `skills/__init__.py:271` `class SkillRegistry`、`:34` `class SideEffect`（`:37` `READ_ONLY` 等副作用标注枚举）；具体技能见 `skills/builtin.py:22` `default_skill_registry` | 类型化参数校验 + 副作用标注的技能注册表；MCP 侧的风险标注是独立的 `ToolSafety`（见下「协议」），二者语义对应但类型不同 |
| 记忆 | `agent/episodic.py:251` `persist_run_embedding`、`:349` `recall_similar_runs`、`:387` `recall_reflections`、`:432` `recall_agent_memory`（CLI 与评测共用的装配点，`--no-recall` 就是它的 `enabled=`）；三因子检索里 D1 recency 读 `last_accessed_at`、D2 按 `world_id` 命名空间过滤、D3 importance 读 `:226` `run_audit_signal` 落在 `agent_runs.audit_signal` 上的确定性审计信号；`assist/lessons.py:41` `extract_lessons_from_report`（确定性蒸馏，无 LLM） | 与官方 `create_agent(store=BaseStore)` 的关系见上「自研决策表」末行 |
| 子 agent | `multi_agent/session.py:99` `_OrchestrationState`（TypedDict，图通道）、`:124` `class MultiAgentSession` | StateGraph + Send + reducer 编排；业务内容走 SQLite 黑板（`multi_agent/blackboard.py`，见上「自研决策表」倒数第二行） |
| HITL | `agent/react.py:593` `HumanInTheLoopMiddleware(interrupt_on=...)`（挂在 `:591` `if self.interrupt_before:` 分支内） | T6 形态 a：`after_model` 拦截，标准 interrupt 三态（approve/reject/edit），`interrupt_before=None` 时不挂，产出字节不变 |
| 协议 | `mcp_server/transport.py:132` `create_mcp_server`；`mcp_server/tools.py:56` `TOOL_SAFETY` 字典（`:39` `class ToolSafety`） | 官方 FastMCP SDK；工具实现体在 `tools.py`，`skills/` 是同一批实现的技能侧镜像 |
| 可观测 | `llm/otel_bridge.py:353` `invoke_agent_span`、`:383` `gen_ai_chat_span`、`:447` `execute_tool_span`（OTel GenAI 语义约定）；`prompts/__init__.py:415` `prompt_registry()` | span 覆盖模型调用/工具调用两级；prompt 侧用注册表防漂移（`tests/agent/test_prompt_registry.py` 断言新 prompt 必登记） |
| 评测 | `evaluation/agent_eval.py:82` `run_jd_dimension_scorecard`（CI 门禁见 `.github/workflows/ci.yml:43-44` `eval-agent`）；`evaluation/agent_task_bench.py:1445` `run_agent_task_bench`（种子世界任务级基准，`eval-agent-tasks`，离线/真实两态，**不在 CI 里**——真实模式花钱）、`:1752` `run_memory_ab`（开/关记忆成对基准，`--memory-ab`，口径 pass@1） | 五维度 scorecard 是回归门；任务级基准是更细的种子错误世界评测,手动跑 |
