import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const source = await readFile(new URL("../src/pages/AskPage.vue", import.meta.url), "utf8");

test("world Q&A can inspect the raw context pack with an explicit token budget", () => {
  assert.match(source, /async function previewContextPack/);
  assert.match(source, /context:pack/);
  assert.match(source, /query, budget_tokens: budget/);
  assert.match(source, /function normalizedContextBudget/);
  assert.match(source, /Number\.isInteger\(value\) && value >= 1 && value <= 8000/);
  assert.match(source, /budget === null/);
  assert.match(source, /v-model\.number="contextBudget"/);
  assert.match(source, /预览原始证据包/);
  assert.match(source, /contextPack\.hits/);
  assert.match(source, /hit\.ref/);
  assert.match(source, /hit\.body/);
  assert.match(source, /hit\.score\.toFixed\(3\)/);
});

test("world Q&A surfaces degraded retrieval and locatable claim citations", () => {
  assert.match(source, /degradation_reasons/);
  assert.match(source, /检索已降级/);
  assert.match(source, /source_preview/);
  assert.match(source, /citation\.page/);
  assert.match(source, /citation\.bbox/);
  assert.match(source, /citation\.revision/);
  assert.match(source, /citation\.supported_claims/);
  assert.match(source, /citation_existence_only/);
});
