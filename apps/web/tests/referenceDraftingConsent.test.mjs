import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const references = await readFile(
  new URL("../src/pages/ReferencesPage.vue", import.meta.url),
  "utf8",
);
const audit = await readFile(new URL("../src/pages/AuditPage.vue", import.meta.url), "utf8");
const review = await readFile(new URL("../src/pages/ReviewPage.vue", import.meta.url), "utf8");

test("reference intake explicitly grants drafting use through both upload doors", () => {
  assert.match(references, /const newAllowDrafting = ref\(false\)/);
  assert.match(references, /\["inspiration", "drafting"\]/);
  assert.match(references, /allowed_uses: selectedAllowedUses\(\)/g);
  assert.match(references, /v-model="newAllowDrafting"/);
  assert.match(references, /允许任务、人设与对白草稿借鉴/);
  assert.match(references, /s\.allowed_uses\?\.includes\('drafting'\)/);
});

test("prompt-injection findings are quarantined and require explicit approval", () => {
  assert.match(references, /created\.injection_flagged_chunks\.length/);
  assert.match(references, /命中间接提示注入规则/);
  assert.match(references, /function isQuarantined/);
  assert.match(references, /source\.quarantine_status === "quarantined"/);
  assert.match(references, /当前未进入任何检索或生成链路/);
  assert.match(references, /人工复核并批准/);
  assert.match(references, /references\/\$\{encodeURIComponent\(id\)\}:approve/);
});

test("structured reference upload preserves locator contract and surfaces degradation", () => {
  assert.match(references, /const BINARY_EXTS = \["\.docx", "\.pdf", "\.xlsx", "\.epub"\]/);
  assert.match(references, /content_base64: await fileToBase64\(newFile\.value\)/);
  assert.match(references, /ingestion_mode\?: "structured_locator"/);
  assert.match(references, /created\.ingestion_mode === "structured_locator"/);
  assert.match(references, /页码\/单元格定位与不可变文件版本已保留/);
  assert.match(references, /本次使用退化解析/);
});

test("audit warning filter sends the backend warning enum", () => {
  assert.match(audit, /\['all', 'error', 'warning', 'info'\]/);
  assert.doesNotMatch(audit, /\['all', 'error', 'warn', 'info'\]/);
  assert.match(audit, /onMounted\([\s\S]*void loadIssues\(\)/);
  assert.match(audit, /nextIssuesCursor\.value = body\.next_cursor;\s*ran\.value = true/);
  assert.match(audit, /已读取 \{\{ issueTotal \}\} 条持久化问题/);
});

test("review queue localizes quest logic drafts", () => {
  assert.match(review, /quest_logic_draft: "任务逻辑草稿"/);
  assert.match(review, /temporal_spec_draft: "时序规约草稿"/);
});
