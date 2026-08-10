import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const pages = Object.fromEntries(
  await Promise.all(
    ["ReferencesPage", "LocalizationPage", "DialoguesPage", "HistoryPage"].map(
      async (name) => [
        name,
        await readFile(new URL(`../src/pages/${name}.vue`, import.meta.url), "utf8"),
      ],
    ),
  ),
);

function extractFunction(source, name) {
  const match = new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\(`).exec(source);
  assert.ok(match, `${name} must exist`);
  const open = source.indexOf("{", match.index);
  let depth = 0;
  for (let index = open; index < source.length; index += 1) {
    if (source[index] === "{") depth += 1;
    if (source[index] === "}") depth -= 1;
    if (depth === 0) return source.slice(match.index, index + 1);
  }
  throw new Error(`Could not find the end of ${name}`);
}

test("reference and snapshot lists request bounded cursor pages and expose continuation", () => {
  const references = pages.ReferencesPage;
  const history = pages.HistoryPage;

  assert.match(extractFunction(references, "refresh"), /limit: "30"/);
  assert.match(extractFunction(references, "refresh"), /params\.set\("cursor", cursor\)/);
  assert.match(extractFunction(references, "loadMoreSources"), /refresh\(cursor, true, epoch\)/);
  assert.match(references, /aria-label="灵感库分页"/);
  assert.match(references, /let sourcesLoadEpoch = 0/);
  assert.match(references, /e instanceof ApiError && \[409, 422\]\.includes\(e\.status\)/);

  assert.match(extractFunction(history, "loadSnapshots"), /limit: "30"/);
  assert.match(extractFunction(history, "loadSnapshots"), /params\.set\("cursor", cursor\)/);
  assert.match(extractFunction(history, "loadMoreSnapshots"), /loadSnapshots\(cursor, true, epoch\)/);
  assert.match(history, /aria-label="快照分页"/);
  assert.match(history, /let snapshotLoadEpoch = 0/);
  assert.match(history, /e instanceof ApiError && \[409, 422\]\.includes\(e\.status\)/);
});

test("localization and dialogue lists preserve deep links while paging", () => {
  const localization = pages.LocalizationPage;
  const dialogues = pages.DialoguesPage;

  assert.match(localization, /const params = new URLSearchParams\(\{ limit: "40" \}\)/);
  assert.match(localization, /params\.set\("cursor", cursor\)/);
  assert.match(localization, /params\.set\("focus", focusKey\)/);
  assert.match(extractFunction(localization, "loadMoreRows"), /fetchOverview\(cursor, activeFocusKey\.value\)/);
  assert.match(localization, /aria-label="本地化文案分页"/);
  assert.match(localization, /caught instanceof ApiError && \[409, 422\]\.includes\(caught\.status\)/);

  const fetchTrees = extractFunction(dialogues, "fetchTreeList");
  assert.match(fetchTrees, /limit: "24"/);
  assert.match(fetchTrees, /params\.set\("cursor", cursor\)/);
  assert.match(fetchTrees, /params\.set\("focus", focusId\)/);
  assert.match(extractFunction(dialogues, "loadMoreTrees"), /fetchTreeList\(cursor, true\)/);
  assert.match(dialogues, /aria-label="对话树分页"/);
});

test("localization restores assignments from the server overview after refresh", () => {
  const localization = pages.LocalizationPage;
  const assignmentFor = extractFunction(localization, "assignmentFor");
  const saveAssignment = extractFunction(localization, "saveAssignment");

  assert.match(localization, /assignee\?: Record<string, string>/);
  assert.match(assignmentFor, /data\.value\?\.rows\.find/);
  assert.match(assignmentFor, /row\?\.assignee\?\.\[locale\]/);
  assert.doesNotMatch(localization, /confirmedAssignments/);
  assert.match(saveAssignment, /row\.assignee =/);
  assert.match(saveAssignment, /await load\(\)/);
});
