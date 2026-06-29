# P0 规模化改造·程序纲领（7 角色编队）

项目 F:\openworld。目标规模见 scratchpad/SCALE_AUDIT.md（5年/百版本/几十万对象/本地内存受限）。
本程序做 **P0 两项 + 性能层**，分**两个工作组**（用户 2026-06-29 拍板：P0 照做，性能另开专门组）：

**工作组 1（存储 + 生命周期）= 原 P0**
- **#1**：稠密向量检索从"全语料进内存 numpy 矩阵 + 暴力 matmul + 任意改动全量 vstack 重建"换成 **sqlite-vec 磁盘驻留 + int8 + 增量 upsert**（content_vectors + reference_vectors 两表都改）。解决两个 🔴：内存 OOM、全量重建。**无损**。
- **#2**：`ProjectContext.open`（pipeline/project.py + mcp_server/tools.py）从"每个工具调用/每个 ReAct step 全量 load+建图+三套表 DELETE 全量重插+重堆矩阵"改成 **session 级共享单个 ProjectContext + replace_* 改 content_hash/mtime diff 的增量同步**；轻量工具（list_issues）走瘦 SQLite 路径。

**工作组 2（性能 / 搜索加速）= 新增**
- 解决 sqlite-vec 的 KNN 仍是 O(N) 磁盘暴力扫描这一点：在存储层之上叠**可插拔搜索层**——小 N 走 sqlite-vec 暴力（默认、无损），大 N 走 **usearch ANN（mmap on-disk、量化）**；ANN 的召回损失由已有两阶段 cross-encoder rerank 兜。并评估 **#0 world_id/version 分片**（把每查询 N 从全历史降到当前版本）作为"让暴力也够快"的正交手段（sqlite-vec 已验证支持 PARTITION KEY）。
- **依赖关系**：工作组 2 建立在工作组 1 的存储 + 检索接口之上 → **设计须先划好"存储 vs 搜索策略"的接口边界**，工作组 1 先落存储+接口，工作组 2 再在接口上叠 ANN/分片（两组**接力**为主、接口确定后局部并行）。

## 红线（贯穿）
- **真实落地非搭壳、不静默降级、根因修不打补丁、复用既有抽象别另起一套**。
- **离线 $0 不破**；**Windows 本地可跑**；不引入需要联网/外部服务的依赖。
- **默认行为/正确性零回归**：现 1422 tests + ruff + mypy + eval(8 gates) + 前端 build 必须全绿；检索召回正确性不退化（用已有两阶段 rerank 兜 ANN 召回损失）。
- 任何依赖新增必须先验证可离线安装 + 在本机真能用（不是"理论上支持"）。

## 分支
执行在独立分支 `feature/scale-p0`（off master a231a36），不碰 master。调研/设计阶段只读不入分支。

## 7 角色流水线（按依赖顺序）
1. **调研方法**（只读+探测，不改码）→ 产出 `scratchpad/P0_RESEARCH.md`：钉死方案可行性，给设计选型依据。
2. **设计/方向**（只读）→ 产出 `scratchpad/P0_DESIGN.md`：架构决策 + 接口边界 + 迁移/分步方案 + 测试策略 + 工作分解。【编排者在此把"向量后端选型"这一不可逆决策带回用户确认】
3. **执行对**：3a 执行（按设计写代码，在 feature/scale-p0）；3b 复查反馈（持续 review 3a 的改动：根因? 打补丁? 回归? 红线?）——两者迭代到 3b 通过。
4. **验收**（独立）→ 按设计 + 全门禁 + 规模性质（内存不再 OOM/增量不再全量重建/共享上下文不再每调重开）验收。
5. **监督**（跨阶段）→ 独立把关：防跑偏、防搭壳、守红线、核 HIGH 根因；在设计后与验收后各介入一次。
6. **总结归纳** → 产出改了什么/为什么/结果/对规模目标的意义（求职可讲）。

## 调研 agent 必须钉死的问题（生死项）
- **sqlite-vec 可行性**：本机 Python 的 `sqlite3.enable_load_extension` 是否可用？（很多 Windows 构建禁用）实跑探测：`python -c "import sqlite3;c=sqlite3.connect(':memory:');c.enable_load_extension(True)"`。能否 pip 离线装 `sqlite-vec` 并在本机 load 成扩展？KNN 查询 API、int8/量化、增量 upsert/delete 形态。
- **备选**（若 sqlite-vec 不可用）：usearch（pip、mmap on-disk、量化、Windows 支持？）、LanceDB（pip、列式磁盘 IVF-PQ、Windows？）、numpy.memmap flat（无依赖、磁盘驻留但仍 O(N) 搜索——只解内存不解搜索成本）、faiss-cpu（Windows wheel？离线？）。逐个验证**离线可装 + 本机真能 import/跑**，给推荐与理由。
- **增量同步模式**：replace_content_index/replace_graph_edges/replace_reference_index 现在全 DELETE+重插（storage/sqlite.py），怎么改成 diff（content_hash/mtime）→ upsert changed + prune removed，事务内安全。
- **共享 ProjectContext 模式**：现 mcp_server/tools.py 每工具各 open。怎么做 session 级共享（参考 pipeline/project.py 的 qa_context_builder 注释）；工具如何接受注入的 context；轻量工具瘦路径怎么走。
