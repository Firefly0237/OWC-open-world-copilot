<div align="center">

# OWCopilot

**面向游戏叙事内容生产的本地优先 AI 工作台。**

将文稿、设计配表和数字版 PDF 转换为可追溯的世界模型，在一套工作流中完成设定检索、
一致性检查、变更影响分析、受控 Agent 协作、提案评审与生产格式导出。

[English](README.md) | 简体中文

[![CI](https://github.com/Firefly0237/OWC-open-world-copilot/actions/workflows/ci.yml/badge.svg)](https://github.com/Firefly0237/OWC-open-world-copilot/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/Python-3.11%20%7C%203.12-blue)
![Node](https://img.shields.io/badge/Node-22-339933)
![License](https://img.shields.io/badge/License-MIT-lightgrey)

<img src=".github/assets/overview.png" alt="OWCopilot 世界总览" width="920">

**知识可追溯 · 一致性自动化 · 受控 AI 协作**

</div>

## 为什么需要 OWCopilot

大型叙事项目的世界规则通常分散在剧本、表格、任务数据、对话树和参考资料中。随着内容增长，
团队需要花费越来越多的时间确认最新设定、追踪改动影响，并避免剧情、策划数据和引擎数据之间
出现矛盾。

OWCopilot 围绕这条生产链路建立统一的内容层：

- 摄取时保留来源定位、内容哈希与版本信息；
- 联合词法、语义和图检索生成带引用的回答；
- 用确定性规则检查引用、时间线、任务逻辑与内容完整性；
- 由有界 Agent 调查问题并生成修改提案；
- 在任务适合拆解时组织多个专职 Worker 协作；
- 通过评审控制正典变更，并向下游工具导出内容。

## 核心能力

| 领域 | 提供的能力 | 主要实现 |
|---|---|---|
| 内容摄取 | DOCX、XLSX、Markdown、JSON/CSV 和数字版 PDF 摄取，保留稳定哈希与来源定位 | [content](src/owcopilot/content/)、[PDF intake](src/owcopilot/intake/) |
| RAG | 结构感知与语义分块、BM25 + dense + graph、weighted RRF、神经重排、上下文预算、引用与拒答 | [retrieval](src/owcopilot/retrieval/) |
| Agent Runtime | 有界 LangChain/LangGraph ReAct、类型化工具、重试、超时、预算、无进展停止、Checkpoint 与评审暂停 | [agent](src/owcopilot/agent/)、[skills](src/owcopilot/skills/) |
| Memory | Checkpoint 工作状态、Episodic/Reflection 检索和人审派生 Lesson Memory，各自采用独立生命周期 | [episodic memory](src/owcopilot/agent/episodic.py)、[reflection](src/owcopilot/agent/reflexion.py) |
| MCP | 本地 stdio Server/Client，支持 tools、resources、prompts、发现与 schema 校验 | [server](src/owcopilot/mcp_server/)、[client](src/owcopilot/mcp_client/) |
| Multi-Agent | Orchestrator–Worker–Verifier 图、作用域任务、SQLite Blackboard、CAS 认领、结果汇总与独立复核 | [multi-agent](src/owcopilot/multi_agent/) |
| 质量与追踪 | 确定性审计、影响分析、版本化评测套件、类型化停止原因和 OpenTelemetry Span | [audit](src/owcopilot/audit/)、[evaluation](src/owcopilot/evaluation/) |
| 交付 | Web、CLI、REST/SSE、Unreal DataTable 导出和 Unity 内容导入包 | [web](apps/web/)、[integrations](integrations/) |

## 快速开始

前置条件：Git、Python 3.11 或 3.12；只有启动 Web 工作台才需要 Node.js 22.12+。系统冒烟流程使用
确定性离线模型，无需 API Key，也不会产生付费模型调用。

~~~powershell
git clone https://github.com/Firefly0237/OWC-open-world-copilot.git
cd OWC-open-world-copilot
python -m venv .venv

# Windows PowerShell
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -e ".[dev,serve,mcp,pdf,tokenizer]"

# 只下载锁定 revision 的 bge-m3 tokenizer（不下载模型权重），再证明它可离线使用。
# 根目录 model-revisions.json 与 Python 包内携带的正本 Manifest 保持一致。
.\.venv\Scripts\python scripts\prefetch_models.py --component tokenizer
$env:HF_HUB_OFFLINE = "1"
$env:TRANSFORMERS_OFFLINE = "1"
.\.venv\Scripts\python scripts\prefetch_models.py --component tokenizer --offline

# 原生工具路径
.\.venv\Scripts\python scripts\run_system_demo.py --transport native --artifact-root .tmp\system-demo

# 通过本地 stdio MCP Server/Client 执行同一审计
.\.venv\Scripts\python scripts\run_system_demo.py --transport mcp --artifact-root .tmp\system-demo
~~~

macOS/Linux 请把 <code>.\.venv\Scripts\python</code> 替换为
<code>.venv/bin/python</code>，并使用 <code>/</code> 路径分隔符。

成功运行后，输出 JSON 将包含：

~~~json
{
  "demo_status": "passed",
  "pdf_rag": {
    "citation_round_trip": true
  }
}
~~~

该命令会贯通 CLI、存储、Checkpoint、Memory、Multi-Agent 图、PDF→RAG 引用回路，以及所选的
原生或 MCP 工具路径。参考结果见[系统演示结果](benchmarks/evidence/system-demo/README.md)。

上述命令就是新机器从零安装路径。Web 开发与生产式联调见
[Web 工作台说明](apps/web/README.md)，PostgreSQL Team 部署见
[Team 运维手册](infra/team/README.md)，真实 Provider 只按 [`.env.example`](.env.example) 显式启用。

## 系统架构

~~~mermaid
flowchart TD
    I["DOCX · XLSX · Markdown · 数字版 PDF"] --> P["摄取：原始字节/hash + 页/单元格/段落定位"]
    P --> C["正典内容：本地文件/SQLite<br/>团队 revision kernel：PostgreSQL"]
    P --> R["检索单元 + parent 关系 + provenance"]
    R --> H["BM25 + dense vector + graph expansion"]
    H --> F["Weighted RRF + cross-encoder rerank + token budget"]
    F --> A["带引用问答 / 有界 ReAct Agent"]
    M["Checkpoint 状态<br/>Episodic/Reflection + Lesson Memory"] <--> A
    X["MCP Client ↔ stdio Server<br/>tools · resources · prompts"] <--> A
    A --> Q{"任务适合拆解？"}
    Q -->|是| MA["Orchestrator → scoped Workers → Verifier"]
    Q -->|否| D["审计与影响分析"]
    MA --> D
    C --> D
    D --> G["带引用回答或修改提案"]
    G --> V["评审门"]
    V --> C
    A -. "预算 · 重试 · stop reason · OTel" .-> T["Trace 与评测"]
    MA -. "assignment · result · verdict" .-> T
~~~

## 主流程如何工作

1. 摄取层保留原始哈希，以及段落、单元格、页面、文本行或边界框定位。
2. 分块先服从文档结构，再在安全结构区域内应用语义边界。
3. BM25、Dense Vector 和 Graph Expansion 生成候选，Weighted RRF 与 Cross-Encoder 完成排序。
4. Context Packing 执行 Token 预算，同时保留生成引用所需的 Provenance。
5. Agent 在重试、时限、预算和无进展控制下调用类型化原生工具或 MCP 工具。
6. 复杂任务可以分发给作用域隔离的 Worker，Verifier 在汇总前重新执行确定性检查。
7. 通过评审的提案才会更新正典内容，并进入引擎导出流程。

## 技术设计

### 检索与分块

结构边界用于保护标题、表格、任务步骤、对话轮次和引用定位。语义相似度只在这些安全区域内决定
合并或切分，既保持局部意图连贯，也不跨越已知文档结构。混合检索以 BM25/Dense 等权作为冷启动，
RRF 消除不同评分尺度的影响，再通过 dev 集选择候选深度、融合权重与重排配置，最后在 held-out 集评测。

### Agent、Memory 与恢复

Agent 获得限定作用域的读取、分析和提案工具。Middleware 对错误分类，对瞬时故障执行有界退避重试，
并实施时间、Token 和无进展停止条件。Checkpoint 用于恢复活动工作流；Episodic 与 Reflection Memory
按相关性、时效性和重要度检索相似运行；通过评审的反馈会形成可撤销的 Lesson。

### MCP 与多 Agent 协作

原生工具与 MCP 工具复用类型化业务 Handler。MCP 层负责协议发现、Resource/Prompt 访问、输入输出
Schema 校验和传输故障观测。面对可分离的子问题，Orchestrator 将作用域明确的任务发布到 Blackboard，
Worker 通过原子操作认领，Verifier 检查结果后再统一汇总；简单任务继续走单 Agent 路径。

时序性质编写也采用同样的可复核设计：受限翻译、封闭世界 flag 接地、确定性组装/类型检查、回译，
以及独立的审阅事务。实现与可执行契约分别位于
[规约编写流水线](src/owcopilot/spec_authoring/) 与 [契约测试](tests/core/test_spec_authoring.py)。

## 基准与质量

| 基准 | 数据与方法 | 当前结果 |
|---|---|---|
| [检索](benchmarks/evaluation/reports/retrieval-report-v1.json) | 24 组冻结文档/查询；12 dev + 12 held-out；bge-m3 与 bge-reranker-v2-m3 | held-out 神经重排 MRR、nDCG@5、Recall@1：**1.0** |
| [冻结 ContextPack 诊断](benchmarks/evaluation/reports/retrieval-diagnostics-v1.json) | 120 条冻结查询；production ContextPack recall、FP2/FP3 预算归因和配对 McNemar A/B；确定性 HashingEmbedder | `$0` **机制验证通过**；绝对指标明确不作为语义质量结论 |
| [分块](benchmarks/evaluation/reports/chunking-report-v1.json) | 6 个边界标注文档；3 dev + 3 held-out；结构与语义两种策略 | held-out balanced accuracy：两种策略均为 **0.75** |
| [结构复杂度](benchmarks/complexity_corpus.md) | 三档 profile 采样叙事语料依次经过 production audit、simulation、有界探索与 witness replay | 三档均未截断，witness replay 全部通过 |
| [正式评测套件](benchmarks/formal/) | 版本化 Contract、Control、Fault、Hard-Zero 规则和产物校验 | 可执行的 Suite 与 Artifact 校验定义 |

两条确定性的 `$0` 诊断无需下载模型即可复算：

~~~powershell
.\.venv\Scripts\python scripts\run_frozen_retrieval_diagnostics.py
.\.venv\Scripts\python scripts\run_complexity_benchmark.py
~~~

安装语义与 tokenizer 依赖后可复算检索与分块报告。首次预取会下载锁定 revision 的 embedding 与
reranker 权重，体积可能达到数 GB，且不会进入 Git；第二条命令会在断网模式下检查缓存是否完整。

~~~powershell
.\.venv\Scripts\python -m pip install -e ".[semantic,tokenizer]"
.\.venv\Scripts\python scripts\prefetch_models.py --component all
.\.venv\Scripts\python scripts\prefetch_models.py --component all --offline
.\.venv\Scripts\python scripts\eval_retrieval_benchmark.py --output .tmp\retrieval-report.json
.\.venv\Scripts\python scripts\eval_chunking_benchmark.py --output .tmp\chunking-report.json
.\.venv\Scripts\python scripts\run_formal_evals.py --output-dir .tmp\formal-evals
~~~

真实 LLM 是独立的按需开关：安装 `.[live]`，把 `.env.example` 复制为 `.env`，配置受支持的
Provider，设置 `OWCOPILOT_LLM_MODE=real`，并用 `OWCOPILOT_API_KEY` 保护服务。默认冒烟、测试和
正式评测均不会调用付费 Provider。

## 启动 Web 工作台

前端需要 Node 22.12+。开发模式请在两个终端分别启动 API 与 Vite；Vite 会把 API 请求代理到
8000 端口。

~~~powershell
.\.venv\Scripts\python -m uvicorn owcopilot.service.api:create_app --factory --port 8000
npm ci --prefix apps/web
npm run dev --prefix apps/web
# 打开 http://localhost:5173
~~~

若要按生产方式使用单端口，请执行 `npm run build --prefix apps/web`，重启 API 后打开
<http://localhost:8000>；也可直接执行 `docker compose up --build`。详见
[Web README](apps/web/README.md)。PostgreSQL Team Profile 使用独立、故障关闭的
[运维手册](infra/team/README.md)，本地快速开始不会静默开启 Team 模式。

可用接口：

- **Web**：摄取、浏览、审计、带引用问答、Agent 任务、评审、可视化和导出。
- **CLI**：面向本地自动化的机器可读命令与非零失败退出码。
- **REST/SSE**：资源化 API 与异步任务进度。
- **MCP**：面向 MCP Host 与 Agent Consumer 的本地工具互操作。

## 仓库结构

~~~text
apps/web/                    Vue 3 工作台
src/owcopilot/agent/         Agent Runtime、Middleware 与 Memory
src/owcopilot/retrieval/     分块、索引、融合、重排与 Context Packing
src/owcopilot/mcp_server/    MCP Server 与共享工具 Handler
src/owcopilot/mcp_client/    MCP Consumer、校验与 Agent Adapter
src/owcopilot/multi_agent/   编排、Worker、Blackboard 与 Verifier
src/owcopilot/evaluation/    验收、任务、可靠性和正式评测 Harness
benchmarks/evaluation/       冻结检索/分块数据集与报告
benchmarks/formal/           版本化评测 Contract 与 Control
scripts/                     系统演示、评测与质量门禁
tests/                       后端与集成测试
infra/ · integrations/       部署与引擎适配
~~~

## 开发与测试

~~~powershell
.\.venv\Scripts\python -m ruff check src tests scripts
.\.venv\Scripts\python -m mypy src/owcopilot
.\.venv\Scripts\python -m pytest -q
npm test --prefix apps/web
npm run build --prefix apps/web
~~~

Windows 下可用 <code>pwsh ./scripts/ci_gate.ps1</code> 执行聚合质量门。GitHub Actions 覆盖
Python 3.11/3.12、MCP 兼容性、PostgreSQL Migration/RLS、前端测试和容器冒烟测试。

## 技术栈

Python 3.11+ · Pydantic v2 · FastAPI · LangChain/LangGraph · SQLite/FTS5/sqlite-vec ·
usearch HNSW · NetworkX · sentence-transformers · MCP Python SDK · PostgreSQL/Alembic ·
OpenTelemetry · Vue 3 · Vite · TypeScript

## 许可证

本项目使用 [MIT License](LICENSE)。Bug 和功能建议请提交至
[GitHub Issues](https://github.com/Firefly0237/OWC-open-world-copilot/issues)。贡献流程、私密安全报告与
发布历史分别见 [CONTRIBUTING.md](CONTRIBUTING.md)、[SECURITY.md](SECURITY.md) 和
[CHANGELOG.md](CHANGELOG.md)。
