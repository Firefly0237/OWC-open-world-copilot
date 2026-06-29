# R5 末轮 · 文笔策划视角（只懂文笔、不懂技术）

测试者视角：非技术剧情策划。关注：顺不顺手、写作辅助靠不靠谱、报错看不看得懂、术语吓不吓人。
基准：commit 9ecf555（R4 fixes，5 组）。

---

## 一、R4 `timeline_order` 修复回归验证 —— 真修好了，确认 OK

R4 把 4 处「timeline_order 字段名泄漏」改成「时间顺序」。逐处复核前端全部 `timeline_order` 出现点：

- `frontend/src/pages/TimelinePage.vue:743`「时间顺序」(编辑面板标签) —— 中文，OK
- `TimelinePage.vue:534`「给任务填上时间顺序」、`:839`「未定序（缺时间顺序）」—— 中文，OK
- `pageHelp.ts:84`「按时间顺序排到一条轴上」—— 中文，OK
- `ReviewPage.vue:222` `timeline_order` 在 `SKIP_KEYS` 集合里 —— 这是「不给评审看的原始字段名」黑名单，用途是**隐藏**该字段、永不渲染。正确。
- `TimelinePage.vue:8`（代码注释）、`:390/391/397/412/413/420`（发给后端 API 的 JS 对象 key）—— 非用户可见，OK

结论：**R4 修复真实有效，未引入新文案问题。**

---

## 二、新发现的真问题

### 【LOW-MED】ExpandPage「自评精修」把后端英文枚举 verdict 直接显示给策划（同一类泄漏的漏网之鱼）

- 文件/行：`frontend/src/pages/ExpandPage.vue:352`
  ```
  自评精修：{{ result.trail.map((r) => `${r.verdict}(缺口${r.gap_count})`).join(" → ") }}
  ```
- 根因：`r.verdict` 是后端 worldgen 评审枚举，原始取值为英文 `"pass"` / `"revise"`（见 `src/owcopilot/worldgen/critic.py:121,208` 与 `worldgen/models.py:125`）。这里没有像别处那样映射成中文，直接拼进正文。
- 复现：在「扩写工坊」生成内容后，底部「自评精修」一行会显示类似：
  `自评精修：revise(缺口2) → pass(缺口0)`
  非技术策划看到 `revise`/`pass` 英文标识符——正是 R4 在治理的那一类「裸后端标识符泄漏进中文正文」。
- 旁证（全前端别处对同一 verdict 字段都做了中文映射，唯独这里漏了）：
  - `CreationPage.vue:238` → `通过` / `需精修`
  - `TimelinePage.vue:790` → `审计通过` / `修正N项`
  - `ReviewPage.vue:80` `verdictLabel()` → 中文
  - `SweepPage.vue:150` → `待处理` / `建议复查`
- 真 bug vs 设计：**真 bug（文案一致性缺口）**，不是设计。严重度 LOW-MED：不影响功能，但破坏「全中文、不露黑话」的体验基线，且与 R4 修复目标同源。
- 建议修法（与全站一致，不打补丁）：把 `${r.verdict}` 换成 `${r.verdict === "pass" ? "通过" : "需精修"}`，与 CreationPage 同样口径。

---

## 三、压测过、确认 OK 的点（末轮 sign-off）

逐项攻过，未发现新问题：

1. **错误消息体验（api.ts `humanizeError`）—— 做得扎实。**
   - 原始报错只进 console（`console.warn`），用户只见友好中文。
   - 分类引导齐全：未开世界 / 超时 / 限流(429) / 未接模型(401) / 连不上(503) / 网络失败，每条都给「下一步怎么办」。
   - 关键安全阀（`api.ts:62`）：只有当后端 detail「看起来像人写的中文短句」（含 CJK、≤160 字、无 traceback/花括号）才原样透出，否则落回通用引导。**不会把裸 404/英文堆栈/`OWCOPILOT_PROJECTS_JSON` 这类环境变量名甩给策划。** 红线「报错看得懂」达标。

2. **新手引导（GuidedTour.vue）—— 对非技术策划非常友好。**
   全中文；每个功能一句「做什么/何时用」；明确点名「API Key」（策划认得的词）；说明键盘翻页；并安抚「没接模型也能用导入/一致性检查等离线功能」。无黑话泄漏。

3. **字段标签映射（ReviewPage / ReadinessBoard / CommandPalette / Archive / Compliance / Audit）。**
   `kind/status/severity/字段名` 渲染统一走 `LABELS[k] ?? k` 中文映射表（FIELD_LABELS / PROFILE_LABELS / KIND_LABELS / SEV_LABEL / STATUS_LABEL）。映射表覆盖了所有已枚举取值；`?? key` 兜底仅在出现未登记新枚举时才会露原文，属可接受的优雅降级，未发现实际漏网枚举。

4. **其余 verdict 渲染点全部已中文化**（CreationPage / TimelinePage / SweepPage / ReviewPage）——除上面 ExpandPage 一处。

5. **写作辅助链路**（AskPage 世界问答 / CreationPage 创作工坊 / ExpandPage 扩写 / ReviewPage 审阅台）的提示语、空状态、按钮文案均为通顺中文，语气温和（「翻阅档案中」「向世界发问」「逻辑审计通过，已送审阅台等人审落定」）。审阅台对 AI 产物「逐条采纳/退回」+ AI 评审准确度参考，对策划是可理解的协作语言。

6. **CLI 帮助为英文**（`owcopilot --help`）—— 观察项，非 bug。CLI 是面向程序员/流水线的同一契约表面，本测试人格（用前端的非技术策划）不直接碰它；定位合理。（另：`--help` 输出里 `re-audits ��` 是我本机 GBK 控制台把源码的 `—` 破折号显示乱码，源码 `cli/main.py:170` 是干净的 `—`，非源码 bug，与已知 GBK quirk 一致。）

7. **导出页 `f.kind`（ExportPage:173）/ 识别预览 `e.type`、`rel.kind`（RecognizePanel、CharactersPage）渲染原值**——这些位置本就是技术性/数据性上下文（含 sha256、文件路径、「拷贝给程序员/本地化团队」，或策划自己写的关系类型自由文本），原值可接受，非泄漏。仅 `RecognizePanel.vue:378`「已存入 metadata」中的 "metadata" 是一处轻微英文借词，属边角、可不改。

---

## 一句话总结

R4 的 timeline_order 文案修复确认真修好、无回归；全前端再扫一遍只揪出 1 处同类漏网——ExpandPage「自评精修」把后端英文枚举 `pass/revise` 直接显示给策划（`ExpandPage.vue:352`，真 bug、LOW-MED、一行可修），其余错误提示/新手引导/字段标签/写作辅助链路均确认对非技术策划友好，给 sign-off。
