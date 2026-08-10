import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const source = await readFile(new URL("../src/pages/ImpactPage.vue", import.meta.url), "utf8");

test("impact target suggestions are cursor paged without blocking arbitrary object refs", () => {
  assert.match(source, /kind: "entity", limit: String\(ENTITY_PAGE_SIZE\)/);
  assert.match(source, /params\.set\("cursor", cursor\)/);
  assert.match(source, /body\.next_cursor/);
  assert.match(source, /e instanceof ApiError && e\.status === 409/);
  assert.match(source, /@click="loadMoreEntities"/);
  assert.match(source, /list="impact-entity-refs"/);
  assert.match(source, /任意对象引用/);
  assert.doesNotMatch(source, /v-if="entities\.length" v-model="row\.target_ref"/);
  assert.doesNotMatch(source, /`\/projects\/\$\{currentProject\(\)\}\/archive`/);
});
