import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const engineering = readFileSync(resolve(root, "src/pages/EngineeringPage.vue"), "utf8");

test("engineering selectors request three independent bounded catalog pages", () => {
  assert.match(engineering, /const CATALOG_PAGE_SIZE = 40/);
  assert.match(
    engineering,
    /CATALOG_KINDS\.map\(\(kind\) => apiGet<EngineeringCatalog>[\s\S]*catalog\?kind=\$\{kind\}&limit=\$\{CATALOG_PAGE_SIZE\}/,
  );
  assert.match(engineering, /kind,[\s\S]*limit: String\(CATALOG_PAGE_SIZE\),[\s\S]*cursor/);
  assert.match(engineering, /catalog\.value\.quest_ids = \[\.\.\.catalog\.value\.quest_ids/);
  assert.match(engineering, /error instanceof ApiError && \[409, 422\]\.includes\(error\.status\)/);
});
test("each engineering selector exposes its loaded count and continuation", () => {
  for (const label of ["加载更多任务", "加载更多对白树", "加载更多清单"]) {
    assert.ok(engineering.includes(label), `missing ${label}`);
  }
  assert.match(engineering, /catalogLoaded\("quest"\)/);
  assert.match(engineering, /catalogLoaded\("dialogue_tree"\)/);
  assert.match(engineering, /catalogLoaded\("style_rubric"\)/);
  assert.match(engineering, /验证候选，只生成修复计划/);
});
