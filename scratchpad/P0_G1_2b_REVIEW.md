# P0 工作组 1 — 2b 复查报告（复查/反馈 agent，只读）

**对象**：未提交工作树改动（分支 `feature/scale-p0`，HEAD=3ff87ee）
**改动文件**（`git diff --stat`，仅 4 个，未碰 #1/#2a 已提交内容）：
- `src/owcopilot/mcp_server/tools.py`（+93/-…，`_project` shared 分支、新增 `_issues_store` 瘦路径、8 handler 加 `project` 参）
- `src/owcopilot/core/skills/builtin.py`（`default_skill_registry` 加 `project=` + `bind` 注入）
- `src/owcopilot/cli/main.py`（`_cmd_agent`/`_cmd_multi_agent` 接线，纯 try/finally 包裹+缩进）
- `tests/test_shared_project_ctx.py`（新增，未跟踪）

**判定：PASS（带 1 条非阻塞 follow-up，建议补但不挡 2b 合并）**

---

## 逐条核查（结论 + 证据）

### 1. 向后兼容逐字节（硬红线）— PASS
- `_project`（tools.py:271-273）：`shared is None` 时走原 `Path/open/try-yield-finally-close`（L274-284），与改前路径**字节级一致**；`shared is not None` 才 `yield shared; return`（不 open/不 close）。
- handler 体内 `project.xxx` 一字未改（逐个核对 audit/list/build/ask/impact/propose/quality/export）。
- `bind`（builtin.py:54）`partial(tool, ..., project=project)`：`project is None` 时与历史 partial 等价。
- **CLI `_cmd_issues` 不受影响**：它走 CLI 自己的 `_ProjectHandle._project`（main.py:1337-1358），与 `tools._project` 是两个不同函数，没动。
- **service/api 完全绝缘**：`api.py` 用自己的 `_registered_project`（13 处），从不 import `mcp_server.tools`（Grep 确认）。
- **MCP transport 默认路径不变**：`transport.py:42-49` `server.tool()(fn)` 不传 `project=`，外部 MCP 调用得到 `project=None` 自管路径。
- **实跑确认**（独立脚本 PROBE1）：`tools.audit_project(project=None)` → `ProjectContext.open` 计数=1、`close` 计数=1、`open_errors=3`。open+close 行为与现状一致。

### 2. 真复用、非每调重开 — PASS
- **自构序列实跑**（PROBE2，不依赖执行 agent 的测试）：owner `open()` 一次 → 同一 registry 连跑 audit/list_issues/build_context_pack/impact_of/quality_harness 5 个工具 → 自打 patch 的 `ProjectContext.open` 计数 **= 1**。证明 5 次工具调用全部复用注入的 ctx，无一重开。

### 3. 写后可见 — PASS
- **实跑确认**（PROBE3）：共享 ctx 下 audit 前 `list_issues count=0`；`audit_project` persist 3 条；紧接 `list_issues` 经同一 `shared.sqlite_store` 连接读到 `count=3`（=audit 的 `open_errors`）。`_issues_store` shared 分支 yield `shared.sqlite_store`（tools.py:304-306），同一 live 连接，写后立即可见。

### 4. 瘦路径正确性 — PASS
- **tripwire 实跑**（PROBE4，独立 patch `ProjectContext.open` / `project_mod.build_content_graph` / `project_mod.VectorRetriever` 三处当绊线）：`list_issues(project=None)` 跑完 `heavy == []` —— bundle/graph/vector 构造**一个都没触发**；同时返回正确持久化行（count=3，id 集合等于 audit）。
- **空库安全**（PROBE4b）：从未被 full-open 填充的新库 → `count=0, issues=[]`，**不报错**（`SQLiteStore.initialize` 在 connect 时建 `issues` 表）。空被当"空"非当"错"，正确。
- **过滤正确**（PROBE4c）：`severity=""/status=""` 被当 unset（tools.py:69-71 `severity or None`），count 与无过滤一致；`status="resolved"` → 0 行（WHERE 真在跑）；`severity="error"` → >=1。

### 5. 生命周期 / 无泄漏 — PASS
- **`_cmd_agent`**（main.py:695-727）：`project = open()` 后 `try: …(含 ReActAgent.run) finally: project.close()`。agent 抛异常也必关。
- **`_cmd_multi_agent`**（main.py:771-793）：嵌套 try/finally —— 内层 `finally: session.close()`，外层 `finally: project.close()`。session 异常路径仍走到 `project.close()`。
- **Diag/Repair/Verifier 真共享同一 ctx**：`MultiAgentSession.__init__` 把**同一** `registry`（已绑 shared project）传给 4 个 agent（session.py:93/98/103/108）；`scoped_registry` 是非拥有代理，`run` 直接转发 `self._base.run`（skill_scope.py:42-50），不重建 handler。故所有 worker 经同一 ctx，非各自开。
- **无双关/无关错连接**：`MultiAgentSession` 的 blackboard 是**独立** sqlite 连接（默认 `db_path=":memory:"`，CLI 未注入 `conn`，session.py:69/81）；`session.close()` 只关 blackboard 连接（session.py:269-274），与 `ProjectContext.sqlite_store`（磁盘 runtime db）无关，无双 close。

### 6. reload 触发 / 长驻一致性 — PASS（风险已诚实评估为低）
- 会话开头 `ProjectContext.open` 即跑 2a 增量 `replace_*` 同步（project.py:48-53），shared ctx 开局即与磁盘最新持久态一致。CLI 注释（main.py:693-694、769-770）准确，**未夸大**——只声称"session start 一致"，未声称中途自动 re-read。
- **"长驻 ctx 中途磁盘被外部改但不感知"**：确实存在——`open()` 后 bundle/graph 是快照，会话内不重读 content 文件（PROBE6 记录）。**严重度：低**。理由：唯一调用方是单进程、单线程、同步的 CLI 一次任务（`multi_agent` 全是 docstring 提 asyncio，实际无 Thread/asyncio——Grep 确认 agent/ 目录零并发）；一次 `owcopilot agent/multi-agent` 运行期间无外部写者是正常前提。非声明性保证缺失，非缺陷。

### 7. 无回归 + 无越界 — PASS
- `pytest tests/ -q`：**1468 passed, 2 skipped, 7 warnings**（189s）——与声称数字逐字相符。
- `ruff check`（4 个 touched 文件）：All checks passed。
- `mypy`（tools.py / builtin.py / main.py）：Success, no issues。
- `eval-acceptance`：顶层 `passed: true`，全部 check（含 retrieval_hit_rate=1.0、seeded_error_detection、tool_selection F1=1.0）通过。
- `eval-golden`：顶层 `passed: True`，5 check 全过（audit_no_open_errors / retrieval_has_aldric / qa_citation / export_manifest / provenance）。
- **未越界**：`git diff --name-only` 仅 3 文件 + 新测试；storage/ content/store.py / retrieval/vector*.py（#1/#2a）零改动。

---

## 非阻塞 follow-up（建议补，不挡合并）

**F-1（中）：MCP transport 实路径未被测试覆盖，新 `project` 参对 FastMCP schema 生成是潜在风险。**
- file:line：`src/owcopilot/mcp_server/transport.py:42-49`（`server.tool()(fn)`）+ `tests/test_mcp_server_transport.py:29-40`（FakeFastMCP 只记 `func.__name__`，**从不构 schema**）。
- 原因：8 个 handler 新增 `project: ProjectContext | None = None`。真 FastMCP 的 `server.tool()` 在注册时用 `func_metadata`→Pydantic `ArgModelBase`（`arbitrary_types_allowed=True`）构参模型并 `model_json_schema()`。本机模拟该路径（`arbitrary_types_allowed=True` + `model_json_schema()`）对 `ProjectContext`（dataclass，且 `from __future__ import annotations` 使注解为字符串）**抛 PydanticUserError/SchemaError**。本仓未装 `mcp` SDK（可选 `[mcp]` extra），无法在此实测 pin 版真实行为，且现有 transport 测试用假对象绕过了该路径，所以"对真 FastMCP 是否破坏"在 CI 里无人验证。
- 严重度判断：对**当前 $0 离线 / CLI agent 路径无任何影响**（CLI 不经 FastMCP，eval/test 全绿即证）；只在"有人装 `[mcp]` 跑真 transport"时才可能触发。但红线含"不静默降级 / 真实落地"，这是 2b 引入、spec §1B 未提及的回归面。
- 改法（择一）：
  1. transport 注册时显式排除 `project`（如 FastMCP 支持的 `skip_names`，或在 `transport.py` 用 `functools.partial`/wrapper 去掉该参再 `server.tool()`）；
  2. 或把 `_project` 的注入改为非签名方式（contextvar / 线程局部 owner ctx），让 8 个 handler 的 model-facing 签名**完全不变**（更贴合 spec "handler 体内一字不改 + model-facing 参不变"的意图）；
  3. 最低限度：把 `test_mcp_server_transport.py` 的 FakeFastMCP 换成真正构 `model_json_schema()` 的桩（或 import-guard 真 SDK 时跑一次注册冒烟），让此风险在 CI 可见。
- 我的倾向：方案 2 最干净（注入彻底脱离签名，MCP/CLI 双路径都零风险），但方案 1/3 也可。**这一条不阻塞 2b 合并**——它是 transport 表面的预防性加固，不影响当前所有绿门禁。

---

## 一句话结论

PASS —— 6 条硬核查全部独立实跑通过、全门禁绿、未越界；唯一一条非阻塞 follow-up 是 MCP transport 真实注册路径未被测试覆盖、新 `project` 参对 FastMCP schema 生成有潜在（非当前路径）风险，建议按上方 F-1 加固。
