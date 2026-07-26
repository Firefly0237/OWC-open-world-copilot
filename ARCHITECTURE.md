# 架构导览（自用 · 复习项目结构）

> 目的：几个月后重开这个项目，先读这份，快速回到设计心智。
> 代码是按**领域分包**（每个包一份自己的 `models.py`/`offline.py` 等，是刻意的，不是混乱）。
> 下面把 `src/owcopilot/` 的包按**逻辑分层**归类——层只是心智模型，代码仍平铺在 `owcopilot/` 下。

## 一句话

内容以文件为真源 → 建索引与图 → 确定性审计当"世界的 linter"（规则覆盖范围内报的都是真错，
不报≠没错）→ agent 与生成都绕这套规则转 → 人审入库 → 交付数据与本地化。

## 分层与各包职责

### 接口层（外部怎么调）
| 包 | 职责 |
|---|---|
| `service/` | FastAPI REST + SSE，前端与外部集成的主入口 |
| `cli/` | 命令行入口，所有子命令；JSON 输出，可进 CI |
| `mcp_server/` | 工具实现体（`tools.py`）+ MCP 传输层；`skills/` 注册表延迟绑定同一批工具实现（方向：skills 镜像 mcp_server 的实现，不是反过来） |
| `app/` | Web 工作台的动作层（人工编辑 + 审阅决策 + 引擎往返） |
| `pipeline/` | 工作流枢纽（harness/export_gate/ingest 等），被 app/cli/mcp_server/service/evaluation 五方复用 |
| `platform/` | 多租户控制面（auth/tenancy/store），service 依赖 |
| `integrations/` | 引擎远程客户端（UE Remote Control / Remote Execution 薄壳） |

### 内容领域（世界是什么）
| 包 | 职责 |
|---|---|
| `content/` | 内容模型（Entity/Quest/Relation…）、文件存取、版本 overlay、`engine_coerce`(引擎行→v2 Quest) |
| `audit/` | **29 条确定性一致性审计**——规则覆盖范围内的确定性判据（linter 语义），贯穿所有 agent 范式 |
| `graph/` | 内容图 + 社区检测（GraphRAG-style 用） |
| `logic/` | QuestLogic 状态机（阶段/条件/变量） |
| `impact/` | 改动影响面分析（纯图遍历，零成本） |
| `readiness/` | 就绪度评分（完整度，非正确性） |
| `search/` · `templates/` · `localization/` | 字面搜索 · 内容模板 · 本地化导出 |
| `exporters/` · `compliance/` · `collab/` · `inspiration/` | 数据包导出 · 版号合规 · 协作 · 灵感库 |

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
| `trust/` | 溯源(provenance) + 安全(注入扫描/路径守卫) |
| `telemetry/` · `util/` · `fakes/` · `assets/` | 成本遥测 · 通用工具 · 测试假件 · 内嵌 UI 资产 |

## 核心数据流

```
已有素材/新想法/引擎改动
      ↓ ingest / worldgen / extraction
content（文件真源）+ graph
      ↓
audit（29 条确定性规则）——世界的 linter，也是 agent/MCTS/verifier 的 ground-truth 奖励
      ↓
agent 诊断 / 检索问答 / 修复提议（都只读或提议，不直接写库）
      ↓ review_queue（人审）= 唯一写 canon 的路径
入库 → calibration/lessons 回喂 prompt（数据闭环）
      ↓
exporters（数据包）+ localization（CSV/XLIFF）+ 引擎往返
```

## 三条贯穿设计主线（记住这三条就抓住了全局）

1. **确定性审计 = 已编码约束的 ground truth**：不靠模型自评，靠 29 条可复算规则（linter 语义：
   覆盖范围内的确定性判据，不等于全部叙事质量）。它同时是 MCTS 的奖励、
   多智能体 verifier 的裁决、生成-验证闭环的验证器——这是让 LLM 范式可验证的关键。
2. **离线确定性替身 = 可复现测试**：`fakes/` + 各包 `offline.py` 让整套测试不依赖真实模型跑通，
   证明"接线/逻辑正确"（真实质量另看 benchmarks/ 的 live 实跑留档）。
3. **人审 = 唯一写入**：agent 只能读/提议，写 canon 必须过 `assist/review_queue.py`。

## 目录约定

- `src/owcopilot/` 源码 · `tests/` 测试（与源码平铺同名对应）· `frontend/` Vue 工作台
- `docs/` 设计与运行文档 · `benchmarks/` 评测留档（live 实跑点状证据 + 确定性 scale 基准，本地）· `scripts/` 一次性脚本
- `deploy/` 部署产物 · `content/` 测试用金样世界
- `integrations/unreal/OWCopilotBridge/` UE 内容型插件（随 UE 工程发布，不进 wheel）
- `unity/com.owcopilot.content-import/` Unity UPM 包 + `unity/offengine-tests/` 离线 C# 差分测试工程

### 已知取舍（诚实边界）
- 4 组包级依赖环（content⇄audit、graph⇄audit、graph⇄impact、audit→logic→assist→audit）由
  `__init__` 不导出/函数内延迟导入刻意切断，245 个模块导入烟测零失败；根因是三处职责错层
  （graph 内的 *_view 视图构建器实为 app 层、logic/draft.py 的 AI 起草实为 assist 层、
  content/ingest.py 对 audit 规则的上行引用），列为后续结构迁移项而非当前风险。
