import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const [audit, compliance, recognize] = await Promise.all([
  readFile(new URL("../src/pages/AuditPage.vue", import.meta.url), "utf8"),
  readFile(new URL("../src/pages/CompliancePage.vue", import.meta.url), "utf8"),
  readFile(new URL("../src/components/RecognizePanel.vue", import.meta.url), "utf8"),
]);

test("write workflows request summaries before reloading their bounded read models", () => {
  assert.match(audit, /\/audits\?include_details=false/);
  assert.match(audit, /await loadIssues\(\)/);
  assert.match(compliance, /compliance:scan\?include_details=false/);
  assert.match(compliance, /await load\(false\)/);
  assert.match(recognize, /recognize\/mappings\?include_details=false/);
  assert.match(recognize, /mappings\/\$\{encodeURIComponent\(name\)\}\?include_details=false/);
  assert.match(recognize, /await loadTemplates\(\)/);
});
