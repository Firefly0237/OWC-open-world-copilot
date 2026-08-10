import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "typescript";

const source = await readFile(
  new URL("../src/components/RecognizePanel.vue", import.meta.url),
  "utf8",
);

function extractFunction(name) {
  const match = new RegExp(`function\\s+${name}\\s*\\(`).exec(source);
  assert.ok(match, `RecognizePanel must define ${name}`);
  const start = match.index;
  const open = source.indexOf("{", start);
  assert.notEqual(open, -1, `${name} must have a body`);
  let depth = 0;
  for (let index = open; index < source.length; index += 1) {
    if (source[index] === "{") depth += 1;
    if (source[index] === "}") depth -= 1;
    if (depth === 0) return source.slice(start, index + 1);
  }
  throw new Error(`Could not find the end of ${name}`);
}

const helperNames = [
  "proposalPageCount",
  "pageBounds",
  "entityPageItems",
  "relationPageItems",
  "unreviewedItemCount",
  "planForSubmission",
];
const helperHarness = ts.transpileModule(
  [
    "const PROPOSAL_PAGE_SIZE = 25;",
    ...helperNames.map(extractFunction),
    `export { ${helperNames.join(", ")} };`,
  ].join("\n"),
  { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } },
).outputText;
const helpers = await import(`data:text/javascript,${encodeURIComponent(helperHarness)}`);

function entity(index) {
  return {
    id: `entity-${index}`,
    name: `Entity ${index}`,
    type: "npc",
    method: "deterministic",
  };
}

function relation(index) {
  return {
    source: `entity-${index}`,
    target: `entity-${index + 1}`,
    kind: "knows",
    method: "deterministic",
  };
}

test("75 proposals are reachable and item 61 keeps its original relation index", () => {
  const entities = Array.from({ length: 75 }, (_, index) => entity(index + 1));
  const relations = Array.from({ length: 75 }, (_, index) => relation(index + 1));

  assert.equal(helpers.proposalPageCount(entities.length), 3);
  const thirdEntityPage = helpers.entityPageItems(entities, 3);
  assert.equal(thirdEntityPage.length, 25);
  assert.equal(thirdEntityPage[10].id, "entity-61");

  const thirdRelationPage = helpers.relationPageItems(relations, 3);
  assert.equal(thirdRelationPage.length, 25);
  assert.equal(thirdRelationPage[10].index, 60);
  assert.equal(thirdRelationPage[10].relation.source, "entity-61");
});

test("cancelling item 61 excludes it from the exact plan submitted for review", () => {
  const entities = Array.from({ length: 75 }, (_, index) => entity(index + 1));
  const relations = Array.from({ length: 75 }, (_, index) => relation(index + 1));
  const plan = {
    source_format: "table",
    entities,
    relations,
    column_mapping: null,
    columns: [],
    unmapped: [],
    warnings: [],
  };

  const submitted = helpers.planForSubmission(
    plan,
    new Set(["entity-61"]),
    new Set([60]),
  );

  assert.equal(submitted.entities.length, 74);
  assert.equal(submitted.relations.length, 74);
  assert.equal(submitted.entities.some((item) => item.id === "entity-61"), false);
  assert.equal(submitted.relations.some((item) => item.source === "entity-61"), false);
  assert.equal(submitted.entities[60].id, "entity-62");
  assert.equal(submitted.relations[60].source, "entity-62");
});

test("submission gate counts every proposal on pages not yet viewed", () => {
  assert.equal(helpers.unreviewedItemCount(75, new Set([1])), 50);
  assert.equal(helpers.unreviewedItemCount(75, new Set([1, 2])), 25);
  assert.equal(helpers.unreviewedItemCount(75, new Set([1, 2, 3])), 0);
  assert.equal(helpers.unreviewedItemCount(12, new Set([1])), 0);
});

test("component has no silent proposal truncation and discloses expandable warnings", () => {
  assert.doesNotMatch(source, /plan\.entities\.slice\(0,\s*60\)/);
  assert.doesNotMatch(source, /plan\.relations\.slice\(0,\s*60\)/);
  assert.match(source, /尚未核对/);
  assert.match(source, /展开剩余 \$\{hiddenWarningCount\} 条警告/);
  assert.match(source, /if \(!result\.value \|\| !canApplyPlan\.value\) return/);
});

test("mapping templates use bounded cursor pages with race and stale-snapshot recovery", () => {
  const load = extractFunction("loadTemplates");
  const more = extractFunction("loadMoreTemplates");

  assert.match(source, /const TEMPLATE_PAGE_SIZE = 20/);
  assert.match(load, /const sequence = \+\+templateLoadSequence/);
  assert.match(load, /limit: String\(TEMPLATE_PAGE_SIZE\)/);
  assert.match(load, /query\.set\("cursor", cursor\)/);
  assert.match(load, /sequence !== templateLoadSequence/);
  assert.match(load, /page\.next_cursor/);
  assert.match(load, /page\.snapshot_etag/);
  assert.match(load, /e instanceof ApiError && e\.status === 409/);
  assert.match(load, /loadTemplates\(null, false, false\)/);
  assert.match(more, /nextTemplateCursor\.value/);
  assert.match(more, /loadTemplates\(cursor, true\)/);
  assert.match(source, /加载更多模板/);
  assert.match(source, /已加载 \{\{ Object\.keys\(templates\)\.length \}\} \/ \{\{ templateTotal \}\}/);
});

test("mapping mutations ignore legacy full dictionaries and refresh the first page", () => {
  const save = extractFunction("saveTemplate");
  const remove = extractFunction("removeTemplate");

  assert.match(save, /await apiPost<\{ saved: string \}>/);
  assert.match(save, /await loadTemplates\(\)/);
  assert.doesNotMatch(save, /templates\.value\s*=/);
  assert.match(remove, /await apiDelete<\{ deleted: string \}>/);
  assert.match(remove, /await loadTemplates\(\)/);
  assert.doesNotMatch(remove, /templates\.value\s*=/);
});
