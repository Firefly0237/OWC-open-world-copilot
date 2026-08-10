import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const archive = readFileSync(resolve(root, "src/pages/ArchivePage.vue"), "utf8");

test("archive media threads use bounded snapshot cursors", () => {
  assert.match(archive, /const ASSET_PAGE_SIZE = 20/);
  assert.match(archive, /object_ref: refValue,[\s\S]*limit: String\(ASSET_PAGE_SIZE\)/);
  assert.match(archive, /if \(cursor\) params\.set\("cursor", cursor\)/);
  assert.match(archive, /assets\.value = append \? \[\.\.\.assets\.value, \.\.\.body\.assets\]/);
  assert.match(archive, /error instanceof ApiError && \[409, 422\]\.includes\(error\.status\)/);
  assert.match(archive, /已加载 \{\{ assets\.length \}\} \/ \{\{ assetTotal \}\}/);
  assert.match(archive, /加载更多素材/);
});
test("asset paging state is fenced when a detail object changes", () => {
  assert.match(archive, /const requestSerial = \+\+assetsRequestSerial/);
  assert.match(
    archive,
    /activeObjectRef\.value === refValue && assetsRequestSerial === requestSerial/,
  );
  assert.match(archive, /assetsRequestSerial \+= 1;[\s\S]*assetNextCursor\.value = null/);
});
