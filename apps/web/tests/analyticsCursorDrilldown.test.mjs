import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const analytics = readFileSync(resolve(root, "src/pages/AnalyticsPage.vue"), "utf8");

test("analytics loads a bounded summary and independent faction cursors", () => {
  assert.match(analytics, /\/analytics\/summary/);
  assert.match(analytics, /\/analytics\/factions\?limit=30/);
  assert.match(analytics, /underdeveloped=true&limit=20/);
  assert.match(analytics, /query\.set\("cursor", cursor\)/);
  assert.match(analytics, /factions\.value = \[\.\.\.factions\.value, \.\.\.page\.factions\]/);
  assert.match(analytics, /caught instanceof ApiError && caught\.status === 409/);
  assert.match(analytics, /已加载 \{\{ factions\.length \}\} \/ \{\{ factionTotal \}\}/);
});
test("gap counts fetch object ids only when the author expands a drill-down", () => {
  assert.match(analytics, /async function toggleGap\(kind: string\)/);
  assert.match(analytics, /\/analytics\/gaps\/\$\{encodeURIComponent\(kind\)\}/);
  assert.match(analytics, /v-for="\(count, k\) in data\.gap_counts"/);
  assert.match(analytics, /:to="gapLocation\(String\(k\), id\)"/);
  assert.match(analytics, /加载更多对象/);
});
