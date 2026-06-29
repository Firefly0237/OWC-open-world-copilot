# P0 工作组 1 — 监督 agent 红线把关（只读，全局视角）

**对象**：分支 `feature/scale-p0`，三个已提交：`515eb74`(#1 sqlite-vec backend)、`3ff87ee`(#2a 增量同步)、`059ce92`(#2b 共享 ctx)。工作树干净（`git status` 空）。
**方法**：不重复单元复查、不跑全量 pytest（验收 agent 并行跑）。做针对性 spot-check（读关键路径代码 + 4 个独立实跑探测）+ 跨单元叠加验证 + F-1 实测裁断。
**对标**：`P0_PROGRAM.md`(红线/F-1)、`P0_DESIGN.md`(§0/§1/§3)、三份复查报告。

---

## 一、红线逐条结论（全局核）

### ① 真实落地非搭壳 — PASS
- **#1 真用 sqlite-vec**：实跑 `ProjectContext.open` → `context_builder.vector.search()` 后 `type(vr._backend).__name__ == 'SqliteVecBackend'`（非偷偷 numpy）。vec0 表 `content_vec` 实际有 2 行 = 语料数，KNN 走 `embedding MATCH ? AND k=?`（vector_backend.py:252-256）。
- **#2b 共享 ctx 真复用**：`_project` 的 `shared is not None` 分支 `yield shared; return`（tools.py:271-273），不 open/不 close。skill 的 `bind` 用 `partial(..., project=shared)`（builtin.py:54）注入同一 ctx。
- **#2a 真增量**：跨单元实跑（见二）证明编辑 1/2 对象 → 正好 1 upsert / 0 delete，非"包一层假增量"。
- **无搭壳**：vec0 表实在磁盘 DB、blob 表实为权威 fp32 源、增量 diff 实按 row_hash/fingerprint/text_hash 算。

### ② 不静默降级 — PASS
- `make_vector_backend`（sqlite.py:815）`except (SqliteVecError, sqlite3.OperationalError)` → `logger.info` 引导日志 + 返回 `None` → retriever 退 `NumpyMatrixBackend`。非裸崩、非静默吞。
- `SqliteVecBackend._load_extension`（vector_backend.py:181-202）import/load 失败 → 转 **guided** `SqliteVecError`（含安装指引）。
- `_sync_backend`（vector.py:264-268）：backend 为 None 时显式落 numpy，非吞。
- replace_* 三个均 `try/except: rollback(); raise`（事务内、异常上抛非吞，sqlite.py:747-750/991-994/参考 source 同形）。

### ③ 根因修非打补丁 — PASS
- backend 抽象干净：`VectorSearchBackend` Protocol（upsert/delete/search/vector_for/clear），retriever 只跟接口说话，两 backend 同模块。
- 指纹 diff 是真根因修：`_edge_fingerprint = sha1(source|target|kind|edge_type|valid_from|valid_until|#occurrence)`（sqlite.py:1471-1487），occurrence ordinal 解决 MultiDiGraph 平行边无自然键——这是给"无 key 表"造稳定 key 的正解，非 hack。
- 共享 ctx 注入是签名级的干净注入（见 ④/F-1），非全局变量 hack。

### ④ 复用既有抽象别另起一套 — PASS
- **blob 表保留为权威 fp32 源**：`get_vectors`/`upsert_vectors`/`prune_vectors` 原样复用；vec0 首用从 blob 一次性 `_backfill_vec0`（sqlite.py:824-837）。vec0 与 blob 的 prune 用**同一** `removed_refs`/`current_refs` 集协调（vector.py:236-237 prune blob + _sync_backend del vec0），无双套账。
- **text_hash 增量嵌入**原逻辑保留（vector.py:193-199）。
- **#2b 复用 `_registered_project` 先例范式**（注入已开 ctx 由 owner 管生命周期）。
- **row_hash/edge_fingerprint 在位升级**（`_ensure_column` 加 `DEFAULT ''`，旧行视为 changed 首次重戳），未另建表。

### ⑤ 向后兼容（不注入 ctx / 无 sqlite-vec 时行为不变）— PASS
- `_project` `shared is None` 分支与改前**字节级一致**（tools.py:274-284 = 原 open/try-yield-finally-close）。
- `bind` 中 `project=None` 时 `partial` 与历史等价。
- 无 sqlite-vec 时 `make_vector_backend` 返 None → numpy 后端，老环境零破坏。
- 离线 $0 / Windows：sqlite_vec wheel 已 vendored（`scratchpad/wheels/sqlite_vec-0.1.9-py3-none-win_amd64.whl`），本机 import 成功。无新增联网依赖。

---

## 二、跨单元交互结论（#1 + #2a + #2b 三者叠加）

**独立实跑**（proper content model，编辑 1/2 对象后 reload，instrument vec0 backend upsert/delete 计数）：

| 检查 | 结果 |
|---|---|
| 长驻 open 后 backend 类型 | `SqliteVecBackend`（#1 真生效） |
| vec0 行数 == content_index 实体行数 | 2 == 2（**vec0 与 #2a content_index 协调一致**，无漂移） |
| 编辑 1/2 对象 reload 后 vec0 写次数 | **1 upsert / 0 delete**（#2a 增量 + #1 vec0 协调，**非全量重建**） |
| reload 后 vec0 行数 | 仍 2（无孤儿、无 stale） |
| 编辑后 search 反映新内容 | 是（write-then-visible 一致性成立） |

- **共享 ctx 长驻 + 增量同步 + vec0 一致性**：三者叠加无相互踩坑。vector reindex 按 `text_hash` 独立判增量、读 live `content_index` 行，与 #2a 的 `content_index` row_hash diff 各管各的键、互不冲突，end-state 一致。
- **blob 表(#1 保留) 与 reference 增量(#2a)**：blob 表 prune 用与 vec0 同一集合驱动，reference 走独立 `text_hash` 整本 diff（_reference_source_unchanged），两条增量路径无交叉表、不打架。
- **结论：三单元叠加无新增风险面**，跨单元一致性经实测成立。

---

## 三、累积非阻塞项裁断

### fix A（make_vector_backend OperationalError guided 回退）— **真修好，确认关闭**
实跑复现 #1 复查报告建议 A 的原始裸崩场景：vec0 以 dim=8 落盘 → 重开以 dim=4 构造 → `_backfill_vec0` 的 `search(np.zeros(dim))` 触发 vec0 dimension-mismatch `OperationalError`。改后 `make_vector_backend` 捕获并返回 `None`（guided 回退 numpy），**不再裸崩**。实测打印 `dim4 backend: None`。**裁决：真非阻塞，已修，关闭。**

### OBS-1（mtime 纳秒碰撞陈旧读，2a）— **真非阻塞，留待办**
触发需"外部进程改 + mtime_ns 完全不变 + size 字节不变"三条同时成立（构造性极端）。设计 §1B 已声明"content_hash 兜正确性、mtime 兜性能"的分层——本层只做性能快路径，上层 content_hash diff 是正确性兜底；fresh store 永远读盘真值。**裁决：真非阻塞，可留待办（组 2 或日后若需可加内容字节弱校验）。**

### OBS-2（dict 去重语义，2a）— **真非阻塞，留待办**
`replace_content_index` 新路径用 `desired[ref]` dict 去重；`_content_rows` 的 ref 对每类对象唯一，无真实碰撞，行为与旧 `executemany` 无差异。**裁决：真非阻塞，纯记录，无需改。**

### F-1（MCP transport project 参 schema 风险，2b）— **必须本组修（裁断升级）**

> 这是我重点裁断的一条，结论与 2b 复查"非阻塞 follow-up"**部分相左**：风险维度（agent/skill 不受影响）2b 判对，但"可留待办"我**不同意**——证据见下。

**agent/skill 路径确不受影响（实证）**：skill 的 model-facing 参数集来自**显式 `SkillParameter` 元组**（builtin.py:77-81 等），`project` 经 `partial` 绑定、永不进 LLM。CLI/agent 不经 FastMCP。这部分 2b 判断正确，已核实。

**但 F-1 在 MCP transport 面是一个真实、可复现的回归（非"潜在"）**：
- 本机虽未装 `mcp` SDK，但**装了 pydantic 2.13.4**，足以**实测**复现 FastMCP `func_metadata` 的核心步骤。我用 `create_model(..., __config__=ConfigDict(arbitrary_types_allowed=True), project=(ProjectContext|None, None)).model_json_schema()` 模拟注册：
  - **带 `project` 参 → 抛 `SchemaError`**（`Field "project" ... dataclass-args ... Field 'embedder' ... 'cls' must be valid as the first argument to 'isinstance'`）。
  - **去掉 `project` 参（pre-2b 形）→ schema 正常生成**。
- **根因**：`ProjectContext` 是 `@dataclass`（project.py:24-35，含 `embedder: Embedder` 字段）。`arbitrary_types_allowed=True` **救不了 dataclass**——pydantic 把 dataclass 当结构化类型递归进字段，`embedder` 非 isinstance-able 故 `model_json_schema()` 整体抛错。
- **后果**：任何人装 `[mcp]` 调 `create_mcp_server()`，第一句 `server.tool()(audit_project)` 注册即崩（FastMCP 在 `Tool.from_function` 内 eager 调 `model_json_schema()`），**8 个工具全注册不了**。这是 2b 引入、spec §1B 未提及的回归面，**直接撞红线"真实落地/不静默降级"**——MCP transport 产品面从"能起"变"一起就崩"。

**为何不能留待办**：F-1 不是"加固"或"边角"，而是 **2b 把一个本可工作的产品入口（MCP server）改成了启动即崩**。红线明确要求"默认行为/正确性零回归"。虽然当前 $0 离线 CLI 路径全绿（因不经 FastMCP），但"MCP transport 能起"是 2b 之前就成立的行为，2b 破坏了它。留待办 = 默许一个已知回归带进收口。

**裁决：F-1 必须本组修。** 倾向修法（与 2b 复查方案一致，择一）：
1. **方案 2（最干净，推荐）**：注入脱离签名——`_project` 改 contextvar / owner-scoped 持有，8 个 handler 的 model-facing 签名**完全不含 project**（最贴合 spec "handler 体内一字不改 + model-facing 参不变"原意）。MCP/CLI 双路径都零风险。注意 contextvar 跨线程传播——但 agent/ 目录实测零并发（2b 复查已 grep 确认），单进程同步 CLI 下无 subtlety。
2. **方案 1**：transport.py 注册前用 wrapper/`functools.partial` 去掉 `project` 参再 `server.tool()`（FastMCP 若支持 `skip_names` 亦可）。
3. **方案 3（最低限度，不够）**：仅把 `test_mcp_server_transport.py` 的 FakeFastMCP 换成真构 `model_json_schema()` 的桩让 CI 可见——能暴露但不修复回归，单独不够，须配 1 或 2。

> 补充：我已能用 pydantic 直接复现，**无需装 `[mcp]` 即可加一个回归测试**（构 `arbitrary_types_allowed` model + `model_json_schema()` 断言不抛），建议修复时一并加，堵住 CI 盲区。

---

## 四、防跑偏结论 — PASS

- **无 group-2 scope creep**：grep `int8|usearch|quantiz|ANN|partition key|world_id.*shard` over retrieval/+storage/ 仅命中 docstring（明确写"组 2 再做"），无任何 int8/ANN/分片实现混入组 1。
- **无为过门禁放水**：组 1 fp32 无损（vector_backend.py:14-18 注释 + 复查的逐位对拍测试），召回门 hit_rate=1.0 是经 live SqliteVecBackend 实测，非弱化测试。
- **范围守住**：三单元改动文件与 spec §1A/§1B 一一对应，无越界（各复查 git diff 已确认）。

---

## 收口判定

红线 ①②③④⑤ 全 PASS；跨单元三者叠加一致性实测成立；fix A 真修；OBS-1/OBS-2 真非阻塞可留待办。**唯 F-1 不是非阻塞——它是 2b 引入的、可复现的 MCP transport 启动即崩回归，撞"零回归/真实落地"红线，必须本组修。**

**组 1 有 1 条必须先修：F-1（MCP transport 的 `project` 参使 FastMCP `model_json_schema()` 注册崩溃；建议方案 2 注入脱签名 + 加 pydantic 级回归测试）。修掉 F-1 后组 1 可收口。**
