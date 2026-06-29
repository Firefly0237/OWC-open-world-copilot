# R5 末轮 · 畸形输入/导入·CLI·API 视角（chaos persona）

被测：F:\openworld（包名 `owcopilot`，注意 brief 写的 `openworld` 路径不存在）。
分支 feature/agent-pipeline-enhancements，HEAD = R4 fixes。
重点：压测 R4 把 id 不变量下沉到写盘边界 `content/store.py:_write_json_dir` 的覆盖面。

可复现脚本：`F:\openworld\scratchpad\r5_repro.py`（venv 跑通，输出见下）。

---

## 【HIGH / 真 bug — 新发现】快照读取路径穿越：`load_snapshot` / `bundle_diff` 用未校验的 snapshot_id 拼文件名

- **根因**：R4 把 `{id}.json` 不变量下沉到了 `_write_json_dir`（写盘边界），但 **`content/snapshot.py` 的快照 `{id}.json` 路径完全没走这条边界**，也没有任何校验。
  - `snapshot.py:100` `load_snapshot(store, snapshot_id)`：`path = store.root / _SNAP_DIR / f"{snapshot_id}.json"`
  - `snapshot.py:79` `write_snapshot` 的 `snap_id` 是内部时间戳生成（安全），但 **读取侧 `snapshot_id` 来自用户**。
- **可达性（API，默认本地模式无鉴权）**：
  - `GET /projects/{project}/diff?from=<from_id>` → `build_diff_view_model(from_id=...)` (`view_models.py:396`) → `load_snapshot(store, from_id)`。`from_id` 是裸 `Query(alias="from")`，无校验（`api.py:2291`）。
  - `POST /projects/{project}/snapshots:restore {snapshot_id}` → `restore_snapshot_action` (`actions.py:2764`) → `load_snapshot`。`snapshot_id` 只有 `Field(min_length=1)`（`api.py:581`），无字符校验。
  - 鉴权：`OWCOPILOT_API_KEY` 未设时 `_require_api_key` 是空操作（`api.py:906-912`），即**默认本地优先模式下这两个端点对任何能连到端口的客户端开放、且参数零校验**。
- **证据（r5_repro.py A1 段实跑）**：
  ```
  sid='../secret'             -> TRAVERSAL HIT, 读到 .snapshots 外的 secret.json
  sid='..\\secret'            -> TRAVERSAL HIT
  sid='..\\..\\r5_outside_secret' -> TRAVERSAL HIT, 读到项目根目录之外的文件
  ```
  追加验证 pathlib 绝对路径吞并行为：
  ```
  root/.snapshots/('C:/Windows/win.ini'+'.json') == C:\Windows\win.ini.json
  root/.snapshots/('/etc/passwd'+'.json')        == C:\etc\passwd.json
  ```
  即 `from_id="C:/Windows/某文件"` 会让 pathlib **丢弃整个项目根**，直接读机器上任意 `.json`（唯一约束=后缀 `.json`）。
- **影响**：
  - diff 端点：把任意 `.json` 按 ContentBundle 解析并把结果塞进响应 → 信息泄露（部分受限：非快照结构的 JSON 经 pydantic 宽松解析多半得到近空 bundle，但**穿越可达性本身不受项目根约束**）。
  - restore 端点：把任意 `.json` 当 bundle 加载后 **`save()` 覆盖整个项目内容** → 破坏/越权写入（同样受 JSON 可解析性限制，但路径逃逸真实存在）。
- **真 bug vs 已兜底**：**真 bug**。与 R4 写盘边界同源威胁模型（`{id}.json` 路径逃逸），但 R4 只补了 store 的**写**侧 6 个 per-file 写入器，快照模块（独立的 `{id}.json` 读+写路径）不在覆盖面内，所以被漏掉。git log 显示 snapshot.py 自 5dc497a 起就无校验，是**前轮一直存在、本轮新发现**的残余。
- **修复方向（最小）**：`load_snapshot` 在拼路径前对 `snapshot_id` 走 `_validate_id_chars(..., forbidden=_FORBIDDEN_ID_CHARS)`（已含 `/ \ . :` + `..` + 控制字符 + 长度，正好覆盖相对穿越、Windows 反斜杠、盘符冒号、绝对路径）。`build_diff_view_model` / `restore_snapshot_action` 同样可前置校验。这是「复用现成共享不变量」而非新补丁，符合红线 3。

---

## 末轮 sign-off：以下攻击面我攻过、确认兜底有效（非 bug）

### (a) 写盘边界覆盖面 —— 确认 OK
- **per-file dict 写入器**（entities/regions/quests/pois/dialogues/dialogue_trees/localized_texts）：`_write_json_dir` 在拼 `{id}.json` 前对每个 key 跑 `_validate_id_chars`。实跑 A3：traversal quest id `../../escape` 被拒，报错清晰（指明禁用字符 + context）。✔
- **jsonl/aggregate 写入器**（relations/event_refs/terms/style_guides）：故意 bypass 写盘边界。实跑 A2 确认这些 key（`../../evil`、`..\esc`、`a/b/c`）**只作为 jsonl/json 的 dict key 落进 4 个固定文件名**（`world/relations.jsonl`、`quests/event_refs.jsonl`、`world/terms.json`、`world/style_guides.json`），**绝不拼进文件名/路径**，无法 traversal 逃逸。这是有意设计且安全。✔（注：load 侧用这些 key 重建 dict，但下游永不把 term/style/qer id 交给 per-file 写入器——`save()` 路由确认只走 aggregate 写入器。）

### (b) 误拒攻击 —— 确认无误拒
- 合法 quest_event_ref 合成冒号 id `q1:e1:mentions_event`：实跑 B 段 save+load 完整往返成功，写盘边界**没误拦**（因为它走 aggregate `_write_quest_event_refs`，不经过 `_write_json_dir`）。✔
- normalize 合成路径（B2）：合成冒号 id 正常生成；显式冒号 id `explicit:colon` 被正确拒绝（`allow_synthetic_separator` 只对合成 fallback 放行冒号，显式 id 仍严格）。✔

### (c) 印尼语 id 列 + 显式 locale 白名单 warning —— 触发精准，无误报/漏报
实跑 C 段：
- C1 `id`-only localized_text 行（无其他 locale）：产出 0 行翻译 + **1 条 warning**（该警告的警告了，值不再静默消失）。✔
- C2 `id` + 真实 locale（en/zh）：产出 2 行 + **0 warning**（id 回归结构标识符角色，不该警告的没误报）。✔
- C3 显式 `locale='zz'`（非 ISO）：1 行 + 1 warning。✔
- C4 显式 `locale='zh-CN'`（带 region 子标签）：1 行 + **0 warning**（区域/大小写形式正确放行）。✔
- C5 dialogue 显式 `locale='xx'`：1 warning（dialogue 路径也接同一白名单）。✔
- C6 `id` 值为空串：0 warning（空值本就不导入，不误报）。✔

---

## 诚实结论
- 找到 **1 个新的真 HIGH**：快照读取路径穿越（read/restore 侧 `{id}.json` 漏在 R4 写盘边界覆盖面之外）。与「不静默降级/区分真 bug」红线一致——这是真路径逃逸，非已兜底。
- R4 在我领域内的核心改动（store 写盘 id 不变量、normalize 合成冒号、印尼语/locale warning）**压测全部通过，无回归、无误拒、warning 触发精准**。
- 残余威胁模型很清楚：R4 的写盘边界思路是对的，只是**没覆盖到 snapshot 这条同类 `{id}.json` 路径**——一行 `_validate_id_chars` 复用即可收口。
