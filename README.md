# OWC

作者可调节 AI 参与、理解改动影响，并复用有效协作方式的叙事共创系统。

## 核心机制

- **Agent 可观测性与执行 Harness**：持久化请求、上下文来源、模型及工具事件；在派发和发布边界核验权限、版本与预算，支持按哈希核对本地调用参数。目前通过轮询展示运行中事件，尚未接通模型增量输出。
- **工作图与技能演化**：根据执行反馈提出类型化图修改和参数化经验，通过端口、依赖、能力及来源检查后编译执行；保留父版本、试用范围、实际结果与撤回状态。
- **协作控制**：支持连续委派、先确认大纲和分步确认。阶段约定改变真实调度；当前内容修改、任务内试用与长期方法采用分开处理。
- **类型化叙事 IR 与状态机检查**：在受约束生成链中校验变量类型和条件表达式，将机制与对白转换为可执行分支及效果映射；通过实际运行时的有界路径遍历核验后果结算，支持跨章状态继承和兼容范围内的版本接续。

## 使用入口

| 路径 | 用途 |
|---|---|
| `/creator` | 创作要求、资料、正文、试玩、局部修改与方法选择 |
| `/evolution` | 工作图、执行依据、技能版本及运行诊断 |
| `/worlds` | 创建、导入、导出与切换工作区 |
| `/settings` | 模型连接与本地界面设置 |

新工作区不附带演示内容。生成需要配置真实模型服务；未配置时给出明确错误，不用测试替身填充作品。

## 本地启动

需要 Python 3.11+、Node.js 22.12+。在仓库根目录执行：

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[serve,live]"
npm ci --prefix apps/web
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

编辑 `.env` 配置现有服务商、模型和 API Key，再启动：

```powershell
scripts/start.ps1 -Build -RequireModelConnection
```

默认地址为 `http://127.0.0.1:8765/creator`。可通过 `-Port 8766 -Workspace local` 指定端口和工作区。

`-RequireModelConnection` 只做不携带凭证的连接预检，不发送生成请求，也不证明余额或 API 鉴权有效。不要覆盖已有 `.env`。

也可直接运行：

```sh
python scripts/serve.py --help
python scripts/serve.py
```

直接运行前先完成前端构建：`npm --prefix apps/web run build`。初始化及数据目录以启动器的参数说明为准。`--check` 只初始化工作区并检查配置路径，不启动服务。

## 结构

```text
apps/web/              Vue 界面与前端回归
src/owcopilot/
  workflow/            工作图、执行、方法版本和协作合同
  logic/               状态表达式、编译与试玩
  content/             内容模型、导入与版本
  llm/                 模型协议、调用策略与观测
  service/             HTTP 接口及装配
  app/                 工作区与业务命令
scripts/               正式启动、连接及资源工具
tests/                独立合成夹具与回归检查
var/
  worlds/              本地工作区内容
  runtime/             执行状态和日志
  cache/               检查工具缓存（不提交）
```

其余 Python 模块为上述主线复用的存储、审计、资料和模型基础能力，不作为另一套演示产品入口。

`.venv/`、`apps/web/node_modules/` 和 `.git/` 分别是本地 Python 环境、前端依赖及版本历史，不属于产品源码。构建目录、运行数据和工具缓存已从编辑器默认目录视图中排除；删除这些本地依赖后，需要重新安装才能开发。

## 开发检查

```powershell
.venv\Scripts\python -m pip install -e ".[dev,serve,live]"
.venv\Scripts\python -m pytest -q tests/domain/test_workflow_graph_v2.py tests/platform/test_creator_production_v2.py tests/platform/test_edit_continuity.py tests/platform/test_workflow_stage_changes_v2.py tests/platform/test_task_method_permissions.py
npm --prefix apps/web test
npm --prefix apps/web run build
```

测试使用显式受控输入和临时存储，不自动发起付费模型调用。保留正式回归测试，不将演示故事和冻结展示报告作为产品依赖。

## 数据与执行边界

- 工作区内容、数据库和任务状态保存在 `var/` 或明确配置的位置；凭证仅保存在本地配置，不进入产物或日志。
- 查看记录与重新执行是不同操作；结果未知时先核对，不自动重派。
- 当前作者页每 2.5 秒、诊断页每 6 秒轮询新版事件。已有其他事件流接口不代表新版模型输出已支持流式传输；取消可阻止后继执行和迟到结果发布，不保证立即终止远端模型。
- 候选试用不代表作者采用；技术检查不代表文学意图保持。
- 新版续玩只支持已定义的兼容范围；不支持的结构明确拒绝，原版本不被覆盖。
- 程序工具仅在限定输入输出和隔离执行合同内启用；使用该能力需要本机可用的容器运行环境。
- 工程协议检查与效果比较分开：测试通过不能证明创作质量、效率或作者体验改善；主线缺失的价格信息保持未知，不按零成本计算。真实配对运行和真人体验收益仍需单独验证。
- 测试缓存及受控 Provider 记录不计入真实模型样本，开发者操作不计入作者评价；测量只纳入明确标识来源的注册工作区运行。

开发方式见 [AGENTS.md](AGENTS.md)。本项目采用 [MIT License](LICENSE)。

## 接续工作

以下为待实施事项，不计入已完成功能：

1. 为新版 Harness 提供同源 SSE，按持久事件序号续读，保证观察和重连不触发执行。
2. 接通服务商真实增量输出，区分生成中草稿与正式产物；保持原有预算、权限和取消边界，禁止将完整返回拆字冒充流式输出。
3. 作者页与诊断页共用观察通道，支持工具过程卡、跨任务解绑、去重与有限缓冲，并独立验证浏览器交互。
4. 扩充同输入运行比较的计量合同，冻结全部角色配置，统计调用、token、端到端耗时和复用节点；先做同任务演化与相似新任务复用的配对观察，保留失败、未知和未改善结果。
