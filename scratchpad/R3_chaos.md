# R3 — 畸形输入混沌测试（身份：单纯乱玩的普通用户 / chaos）

被测：`feature/agent-pipeline-enhancements`，已过 R1+R2 修复。
重点：找**绕过 R2 ID 硬化**的新畸形输入，或还没覆盖的入口。

## 先验证 R2 那批修复是不是真修了 —— 是，确认到位
`src/owcopilot/content/normalize.py` + `importers/json.py` + `cli/main.py` 里：
- 控制字符拦截 `ord(c) < 32`（normalize.py:43）✅
- 批次内重复 ID 检测（normalize.py:107 `_check_duplicate`）✅ 实测对 quest_event_ref 也生效（dup 用例正确 raise）
- id 非 str 预检 `_assert_id_is_str_or_none`（normalize.py:69）✅
- JSON 顶层标量 raise（json.py:73 BUG-6）✅
- `impact --max-depth` / 多处 `--budget-tokens` 用 `_nonneg_int`（main.py:457）✅
- `agent --skills` 未知名 warn（main.py:694 BUG-8）✅

**但**：R2 的 ID 硬化是**逐函数**加的，只覆盖了 6/8 个 `_xxx_from_raw`。

---

## 真 BUG-1【高】quest_event_ref 与 style_guide 完全绕过 R2 的 ID 硬化

### 根因
R2 把 `_assert_id_is_str_or_none()` + `_require_valid_id()` 加进了 6 个归一化函数
（entity / quest / region / poi / dialogue / term），但**漏了两个**：
- `_quest_event_ref_from_raw`（normalize.py:252-256）—— 直接 `str(raw.data.get("id") or ...)`，无任何校验
- `_style_guide_from_raw`（normalize.py:459-460）—— `str(raw.data.get("id") or "style_guide")`，无任何校验

没有任何下游 audit 规则补查 ID 类型/控制字符（grep 确认 `_require_valid_id`/`_assert_id_is_str` 只在 normalize.py 出现），
所以这两类内容的畸形 id 一路静默落库。

### 证据（实测，`scratchpad/chaos_repro.py`）
type confusion（= R2 BUG-3/4/5 修过的同一 bug 类，对这俩 kind 仍然成立）：
```
quest_event_ref id=['a','b'] -> 落库 id = "['a', 'b']"     # 列表被 str() 揉成假 id
quest_event_ref id={'x':1}   -> 落库 id = "{'x': 1}"
quest_event_ref id=True      -> 落库 id = "True"
style_guide     id=['a']     -> 落库 id = "['a']"
style_guide     id={'k':'v'} -> 落库 id = "{'k': 'v'}"
```
控制字符 / 路径分隔符 / 路径穿越（= R2 BUG-1 修过的同一 bug 类）：
```
quest_event_ref id="evt\x00ref"        -> 落库（NUL 没拦）
quest_event_ref id="../../etc/passwd"  -> 落库（".." 没拦）
quest_event_ref id="a/b:c"             -> 落库（/ 和 : 没拦）
style_guide     id="sg\x00"            -> 落库
style_guide     id="../../../boom"     -> 落库
```

### 端到端确认（`scratchpad/e2e.py`，已实跑 CLI `ingest --write`）
畸形 id `../../../../evil`、`['list', 'as', 'id']`、`sg slash/colon:bad`
经 `owcopilot ingest --write` **全部静默落盘，exit 0，has_errors=false，issues=[]**：
```
quests/event_refs.jsonl: {"id":"../../../../evil",...}  {"id":"['list', 'as', 'id']",...}
world/style_guides.json: {"sg slash/colon:bad": {...}}
```

### 影响评估（诚实分级）
- **type confusion（list/dict/bool→假字符串 id）**：与 R2 已判定为「真 bug」的 BUG-3/4/5 完全同质——
  静默把数据错误变成一个看起来合法的 id，掩盖上游错误。对这两个 kind 仍 100% 复现。**这是核心残余 bug。**
- **路径穿越**：危害比 per-file kind 低——quest_event_ref/style_guide 写进**聚合文件**
  (`event_refs.jsonl` / `style_guides.json`)，traversal 只污染文件内的 key，不会逃逸成磁盘路径。
  （对比：entity/quest 等按 `{id}.json` 单文件存——见 store.py:138——所以 R2 当时**正是**给这些 per-file kind 加了硬化。
  这俩 kind 没单文件，被漏掉时危害看着小，但 type-confusion 那一面照样严重。）

### 是真 bug 吗
**是。** 不是闸门兜底、不是人审兜底、不是 v1 增强方向——就是 R2 修复时两个函数被漏掉，
且修复模式（一个 `_assert_id_is_str_or_none` + 一个 `_require_valid_id` 调用）现成，照搬即可。

### 建议修法
在 `_quest_event_ref_from_raw` 和 `_style_guide_from_raw` 开头补：
```python
_assert_id_is_str_or_none(raw.data.get("id"), context=f"... from {raw.source_path}")
```
并把最终 id 过一遍 `_require_valid_id(...)`（注意 quest_event_ref 的默认 id 形如 `q1:e1:kind` 含冒号，
若要套用 `_require_valid_id` 需要把这种合成 id 排除或调整禁用字符集——这点要先讨论，别盲目套）。

---

## 真 BUG-2【中】localized_text：任意 2 字母字段名被当成 locale，静默捏造翻译行

### 根因
`normalize.py:399-401` 遍历 raw 的**所有 key**，凡 `_looks_like_locale(key)` 为真就当成一条本地化文本。
而 `_looks_like_locale`（normalize.py:531-535）对任意 `^[a-z]{2}(-[a-z]{2})?$` 都返回 True——
**包括 `id`**（恰好两个字母）和任何手滑的 2 字母列名。

### 证据（`scratchpad/loc_probe.py`，实测）
```
localized_text {"id": {"x":1}, "text_key":"k", "locale":"en", "text":"hi"}
  -> 行1 id=loc_k_en locale=en  text='hi'              # 正常
  -> 行2 id=loc_k_id locale='id' text="{'x': 1}"        # 凭空多出一条 locale=id 的假翻译！

localized_text {"text_key":"k", "zz":"garbage", "qq":"more"}
  -> loc_k_qq locale=qq text='more'                     # 两个垃圾列各捏一行
  -> loc_k_zz locale=zz text='garbage'
```
即：一个本意是 `id` 的字段，因为字面是 2 个字母，被误读成印尼语(id) locale，
把它的值（这里是个 dict 的 str 形）写成了一条本地化文本。静默吞/造数据，无任何告警。

### 是真 bug 吗
**是，但中等。** 触发需 `kind=localized_text` 且存在 2 字母字段名（`id` 是最现实的撞车点）。
属于「过宽的启发式 + 静默」——与用户最在意的「不静默」原则相悖。
建议：locale 识别加白名单或要求显式 `locale=` 字段，至少对 `id`/已知保留字段名排除。

---

## 次要观察【低】深层嵌套 JSON → 原始 RecursionError，非引导式报错

### 根因
`importers/json.py` 捕获了 `json.JSONDecodeError` 并换成友好中文，但没捕获 `RecursionError`。
一个 metadata 嵌套 ~5000 层的畸形 JSON 会让 `json.loads` 抛原始 `RecursionError`。

### 证据
CLI 顶层 boundary（main.py:58）兜住了，不会崩进程，但输出是原始文案：
```
{"error": "maximum recursion depth exceeded while decoding a JSON object from a unicode string",
 "type": "RecursionError"}
```
进程不崩（返回码 2），只是这条不符合「失败别抛原始报错、说清问题+引导下一步」的项目原则。

### 是真 bug 吗
**算半个——已被顶层 boundary 兜底，不致命**，仅是「引导式错误」原则的一个漏网点。
低优先；要修就在 json.py 的 `except` 里也接住 `RecursionError`，给「文件嵌套过深」的引导。

---

## 验证过、确认 OK 的点（验收信号）
- emoji id（`npc_💀🔥`）实体：正常归一化、可入 per-file 名，**非 bug**（之前看到的 GBK 报错是我 print 的、是已知控制台 quirk）。
- 超大整数 `10**400` 作 `timeline_order`：Python bigint 正常吃下，不溢出，**非 bug**。
- per-file kinds（entity/quest/region/poi/dialogue/term）的 id 硬化：控制字符/`..`/`/`/`\`/`:`/非 str/批内重复——**全部正确拦截**，R2 修复对这 6 类扎实。
- JSON 顶层标量、JSONL 单行坏行、字段映射非对象：均有友好中文报错。
- 批内重复 id（含 quest_event_ref）：正确 raise（`_check_duplicate` 对所有 kind 生效，与 ID 内容校验是两条独立链路）。

---

## 复现脚本（留档于 scratchpad）
- `chaos_repro.py` —— BUG-1 全部用例（normalize 层）
- `e2e.py` —— BUG-1 经 CLI `ingest --write` 端到端落盘确认
- `loc_probe.py` —— BUG-2 localized_text 假 locale 行
- `deep.py` —— 深嵌套/大数/emoji
