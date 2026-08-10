import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const source = await readFile(
  new URL("../src/pages/CharactersPage.vue", import.meta.url),
  "utf8",
);

test("character results retain and render reference lineage and leakage findings", () => {
  assert.match(source, /reference_report\?: ReferenceReportRow\[\]/);
  assert.match(source, /referenceReport: source\.reference_report \?\? \[\]/);
  assert.match(source, /referencesRequested:/);
  assert.match(source, /result\.referencesRequested/);
  assert.match(source, /result\.referenceReport/);
  assert.match(source, /row\.source_title/);
  assert.match(source, /row\.transformation/);
  assert.match(source, /row\.leakage\?\.message/);
  assert.match(source, /row\.leakage\.detected/);
});
