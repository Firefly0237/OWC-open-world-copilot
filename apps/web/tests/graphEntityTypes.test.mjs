import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const [graphPage, models] = await Promise.all([
  readFile(new URL("../src/pages/GraphPage.vue", import.meta.url), "utf8"),
  readFile(new URL("../../../src/owcopilot/content/models.py", import.meta.url), "utf8"),
]);

const ENTITY_TYPES = [
  "npc",
  "location",
  "faction",
  "item",
  "event",
  "region",
  "organization",
  "concept",
  "term",
  "skill",
  "achievement",
];

test("graph creation exposes every EntityType accepted by the backend", () => {
  const newTypes = /const NEW_TYPES = \[([\s\S]*?)\];/.exec(graphPage)?.[1] ?? "";
  for (const kind of ENTITY_TYPES) {
    assert.match(models, new RegExp(`= "${kind}"`), `backend EntityType must retain ${kind}`);
    assert.match(newTypes, new RegExp(`"${kind}"`), `graph creation must expose ${kind}`);
  }
});

test("graph labels every backend EntityType in Chinese", () => {
  const labels = /const TYPE_LABEL:[\s\S]*?= \{([\s\S]*?)\n\};/.exec(graphPage)?.[1] ?? "";
  for (const kind of ENTITY_TYPES) {
    assert.match(labels, new RegExp(`\\b${kind}:\\s*"[^"]+"`), `missing label for ${kind}`);
  }
});
