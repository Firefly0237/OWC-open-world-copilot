import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const [characters, overview, readiness] = await Promise.all(
  ["pages/CharactersPage.vue", "pages/OverviewPage.vue", "components/ReadinessBoard.vue"].map(
    (path) => readFile(new URL(`../src/${path}`, import.meta.url), "utf8"),
  ),
);

function extractFunction(source, name) {
  const match = new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\(`).exec(source);
  assert.ok(match, `${name} must exist`);
  const open = source.indexOf("{", match.index);
  assert.notEqual(open, -1, `${name} must have a body`);
  let depth = 0;
  for (let index = open; index < source.length; index += 1) {
    if (source[index] === "{") depth += 1;
    if (source[index] === "}") depth -= 1;
    if (depth === 0) return source.slice(match.index, index + 1);
  }
  throw new Error(`Could not find the end of ${name}`);
}

test("character workshop pages archive entities and exposes inspiration grounding", () => {
  const load = extractFunction(characters, "loadArchive");
  const run = extractFunction(characters, "run");
  const parse = extractFunction(characters, "parseCharacterResult");

  assert.match(load, /kind: "entity", limit: String\(ARCHIVE_PAGE_SIZE\)/);
  assert.match(load, /params\.set\("cursor", cursor\)/);
  assert.match(load, /sequence !== archiveLoadSequence/);
  assert.match(load, /append \? \[\.\.\.archiveEntities\.value, \.\.\.body\.items\]/);
  assert.match(load, /e instanceof ApiError && e\.status === 409/);
  assert.match(characters, /archiveNextCursor/);
  assert.match(characters, /@click="loadMoreArchive"/);
  assert.match(characters, /archiveEntities\.length \}\} \/ \{\{ archiveTotal/);

  assert.match(characters, /v-model="form\.useReferences"/);
  assert.match(run, /use_references: form\.useReferences/);
  assert.match(parse, /inspirationRefs: source\.inspiration_refs \?\? \[\]/);
  assert.match(characters, /result\.inspirationRefs\.length/);
});

test("overview drill-downs fetch one bounded archive kind at a time", () => {
  const load = extractFunction(overview, "loadDrillPage");
  const style = extractFunction(overview, "loadStyleGuides");

  for (const kind of ["entity", "quest", "region", "relation", "poi", "dialogue", "graph_ref"]) {
    assert.match(overview, new RegExp(`archiveKind: "${kind}"`));
  }
  assert.match(load, /kind: tile\.archiveKind/);
  assert.match(load, /limit: String\(DRILL_PAGE_SIZE\)/);
  assert.match(load, /params\.set\("cursor", cursor\)/);
  assert.match(load, /sequence !== drillLoadSequence/);
  assert.match(load, /append \? \[\.\.\.drillRows\.value, \.\.\.rows\]/);
  assert.match(load, /e instanceof ApiError && e\.status === 409/);
  assert.match(overview, /@click="loadMoreDrill"/);

  assert.match(style, /kind: "style_guide", limit: "50"/);
  assert.match(style, /sequence !== styleGuideLoadSequence/);
  assert.match(overview, /@click="loadMoreStyleGuides"/);
  assert.doesNotMatch(overview, /ensureInventory/);
});

test("readiness drawer follows opaque cursors and restarts stale snapshots", () => {
  const load = extractFunction(readiness, "loadReadiness");

  assert.match(load, /only_incomplete: "true"/);
  assert.match(load, /limit: String\(READINESS_PAGE_SIZE\)/);
  assert.match(load, /params\.set\("cursor", cursor\)/);
  assert.match(load, /sequence !== loadSequence/);
  assert.match(load, /items: \[\.\.\.report\.value\.items, \.\.\.body\.readiness\.items\]/);
  assert.match(load, /e instanceof ApiError && e\.status === 409/);
  assert.match(readiness, /@click="loadMoreReadiness"/);
  assert.match(readiness, /incomplete\.length \}\} \/ \{\{ incompleteTotal/);
});
