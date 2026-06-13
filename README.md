# OWCopilot · 开放世界内容工作台

> Open-world game content workbench — 把散落在文档、表格和引擎里的世界观设定整理成机器可读的内容图谱：**查设定有出处，跑审查有证据，AI 产物过人审，引擎导出带校验。**

[![CI](https://img.shields.io/badge/tests-330%20passed-brightgreen)]()
[![python](https://img.shields.io/badge/python-3.11%2B-blue)]()
[![license](https://img.shields.io/badge/license-MIT-lightgrey)]()

当剧本到百万词量级（《博德之门 3》约 200 万词、《赛博朋克 2077》100 万词 × 11 种语言），靠人脑保证设定不互相矛盾在数学上不成立——一处"阵营写反了"的错误会被本地化语言数放大成 N 份返工。OWCopilot 的定位不是"AI 替你写内容"，而是 **"AI 帮你核对、检索、组织内容"**：确定性规则在前、LLM 在后、人审收口、全链路留痕。

---

## 功能特性

| 支柱 | 能力 |
|---|---|
| 🗂️ **内容中枢** | JSON / CSV / XLSX（含中文表头映射）/ Markdown 多源导入，默认 dry-run；内容即文件（git 友好），SQLite 只存可重建的运行态 |
| 🛡️ **一致性审计** | 23 条确定性规则（引用完整性 / 图谱关系 / 世界观语义 / 区域关卡 / 本地化预检 / 注入扫描 / AI 信任），零 LLM、结构化证据；baseline 棘轮模式让存量项目当天接入 CI 门禁 |
| 🕸️ **影响分析** | 改表之前看波及面：纯图遍历输出"必须改 / 建议查"清单，零成本零幻觉 |
| 🔮 **检索问答** | BM25 + 向量 + 实体锚定图扩展三路召回（中文友好），RRF 融合，token 预算器；答案逐条挂引用，**查不到明确拒答，绝不编造** |
| ⚒️ **修复闭环** | issue → 确定性修复器 + LLM 候选 → **影子副本复跑审计**（会引入新错误的候选直接丢弃）→ 人工应用（记录操作者）→ 一键回滚 |
| 🎭 **受约束生成** | 任务草稿与台词变体只引用图谱内实体、生成即审计/lint，全部进**持久化待审队列**——人审采纳是 AI 内容落盘的唯一通道 |
| 📦 **引擎导出** | UE DataTable 兼容 CSV / Unity 每任务 JSON / 通用 bundle，manifest 对每个产物记 sha256 |
| 💰 **成本工程** | 所有模型调用过唯一网关：双层缓存、级联路由、输出 token 上限、逐操作成本回显与预算护栏；离线默认 $0 |

四种使用形态共享同一内核：**Web 工作台（Vue + FastAPI 单进程）/ CLI（CI 门禁）/ REST（服务化）/ MCP（agent 生态，刻意不暴露写动作）**。

## 快速开始

```powershell
git clone <repo> && cd openworld
python -m venv .venv && .\.venv\Scripts\pip install -e ".[dev,serve,app]"

# 五分钟验收：生成内置双语示例世界（雾脊行省），跑完整门禁链
.\.venv\Scripts\python.exe -m owcopilot.cli.main eval-acceptance --workspace .tmp\demo
```

`eval-acceptance` 会生成一个 65 实体 / 10 区域 / 36 任务的双语世界并验证五道质量门禁（零误报、25 个埋错全检出、影响分析零漏报、30 问句检索全命中、问答拒答纪律）。生成的 `.tmp\demo\acceptance_world` 目录可直接作为下面所有命令的 `--content-root`。

### 启动工作台（Vue，主路径）

```powershell
npm --prefix frontend install && npm --prefix frontend run build   # 仅首次
.\.venv\Scripts\python.exe -m uvicorn owcopilot.service.api:create_app --factory --port 8000
# 浏览器打开 http://localhost:8000 —— 单进程、零配置，世界存放在 ~/.owcopilot/worlds/
```

侧边栏按分区组织，覆盖全部能力：**概览**（世界总览 / 设定档案）· **创世·创作**（创世工坊 / 人物工坊 / 创作工坊：任务草稿·对话树·台词·物案）· **内容带入**（文稿提炼 / 表格导入 / 灵感库）· **校勘·分析**（校勘修复 / 影响分析 / 专项清查）· **问答·交付**（世界问答 / 审阅台 / 导出交付）· **管理**（工作区 / 设置）。首次打开有「新手引导」逐区讲解；干净安装会先带你到「工作区」创建第一个世界。

**接入你自己的模型**：打开「设置」选择服务商（DeepSeek / OpenAI / Anthropic / Kimi / 智谱 / 通义 / 豆包 / 自定义）、粘贴你的 API Key、选择模型并「测试连接」，保存后创世 / 人物 / 问答 / 清查全部走真实模型，每次调用的花费在结果与顶栏实时回显。**Key 只进入本机服务进程内存，调用直连服务商，没有任何中间服务器**。不接入也能翻阅档案、审阅、导出。（代码层的离线确定性应答器只服务于测试与 CI——这是 420+ 个 $0 测试的基座，不作为产品功能暴露。）

### 旧版工作台（Streamlit，已弃用）

```powershell
owcopilot ui          # deprecated：仅维护，不再更新；下个版本移除
```

**Vue 版已实现与 Streamlit 工作台的功能对等**（校勘修复 / 影响分析 / 创作工坊 / 文稿提炼 / 表格导入 / 灵感库 / 新手引导全部补齐），Streamlit 版自此弃用：不再有独有功能，问题只在影响数据安全时修复，下个版本移除 `ui` 命令。

### CLI 日常闭环

```powershell
owcopilot audit   --content-root <dir> --fail-on-error          # CI 门禁：有未解决 error 即 exit 1
owcopilot audit   --content-root <dir> --update-baseline b.json # 存量项目：承认现状、只拦增量
owcopilot issues  --content-root <dir> --severity error
owcopilot suggest --content-root <dir> --issue-id <id>          # 影子校验过的修复候选
owcopilot apply   --content-root <dir> --patch-id <id> --operator 你的名字
owcopilot impact  --content-root <dir> --change entity_delete:entity:npc_x
owcopilot ask     --content-root <dir> --query "玄武之约是什么事件？" --llm-mode real
owcopilot draft   --content-root <dir> --brief "护送盐车支线" ; owcopilot review --content-root <dir>
owcopilot export  --content-root <dir> --output-dir out --target-engine unreal
```

所有命令输出 JSON（`--output` 可同时落盘），exit code 规范（0/1/2），LLM 命令支持 `--llm-mode offline|real`、`--max-cost-usd` 预算护栏。

## 部署

```powershell
docker compose up --build       # api:8000 + workbench:8501，离线模式，$0
```

真实模式上线检查单：

1. `.env` 配置 `OPENAI_BASE_URL` / `OPENAI_API_KEY`（OpenAI 兼容端点，如 DeepSeek）——`.env` 被 git/docker 双重忽略，密钥只在运行时注入；
2. **必须**设置 `OWCOPILOT_API_KEY`：未设置时来自**非本机**客户端的 `llm_mode=real` 请求会被直接拒绝（fail-closed，因为它花真钱；本机 loopback 视为机主，不受此限）；设置后所有请求需带 `X-API-Key`；
3. `OWCOPILOT_PROJECTS_JSON='{"demo":"/data/content"}'` 注册项目目录——API 不接受请求体里的任意文件路径；
4. 限流（`OWCOPILOT_RATE_LIMIT_PER_MIN`）与输出上限（`OWCOPILOT_MAX_OUTPUT_TOKENS`，默认 3000）按需调整；多副本部署解开 compose 里的 redis 块共享缓存。

## 架构

```
  Workbench UI      CLI            REST           MCP（7 工具，无写动作）
       └──────────────┴──────┬───────┴──────────────┘
                     pipeline/  固定工作流（audit·patches·review·ingest）
        ┌────────────────────┼──────────────────────┐
   audit/ 23规则      patches/ 建议·影子校验      assist/ 草稿·台词·lint
   impact/ 图传播     qa/ 引用问答·拒答           retrieval/ 三路召回·预算器
        └────────────────────┼──────────────────────┘
   content/ 文件即事实源   graph/ 内容图谱   storage/ SQLite(WAL) 运行态
                     llm/  唯一网关：缓存·路由·遥测·重试·token上限
```

设计纪律（不可逆的顺序）：**确定性校验 → LLM 建议 → 人工确认 → 落盘 → 复跑审计**。每个内容对象携带 `origin`（human / ai_draft / ai_patch）与 `review_status`，未过人审的 AI 内容会被 `UNREVIEWED_AI_CONTENT` 规则持续标记——AI 参与度报告可随时导出，直接服务平台披露要求。

## 质量与评测

```powershell
.\.venv\Scripts\python.exe -m pytest               # 330 个测试，全程离线 $0
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\python.exe -m mypy src\owcopilot
```

- **离线验收基准**（每次 push 跑）：埋错检出率 ≥85% 门禁（实测 100%）、检索命中 ≥90% 门禁（实测 100%）、影响分析零漏报、干净世界零误报；
- **真实模型验证**（手动触发 `.github/workflows/real-llm.yml` 或本地 `scripts/run_real_llm_round2.py`）：对 deepseek-v4-flash 验证问答 / 修复建议 / 草稿 / 台词四条通路，历轮报告与原始结果保存在 [project_docs/](project_docs/)。

## 安全说明

- 离线默认：不配置任何密钥时全部功能可用（确定性 provider），成本为零；
- 真实模式双重 fail-closed（服务级与请求级）；导出路径锁定在项目运行目录内（防穿越）；导入文本全量过 prompt 注入扫描；
- 操作审计：patch 应用/回滚与审核决定均记录操作者与时间；
- **密钥轮换规程**：在 provider 控制台作废旧 key → 更新 `.env` → 重启服务即可，代码无需变更。`.env` 若曾被共享或截屏，应立即轮换。

## License

[MIT](LICENSE)
