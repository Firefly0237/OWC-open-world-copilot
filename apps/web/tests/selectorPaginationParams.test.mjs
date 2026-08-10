import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const expand = await readFile(new URL("../src/pages/ExpandPage.vue", import.meta.url), "utf8");
const palette = await readFile(
  new URL("../src/components/CommandPalette.vue", import.meta.url),
  "utf8",
);
const genesis = await readFile(new URL("../src/pages/GenesisPage.vue", import.meta.url), "utf8");

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

test("expand selector independently follows bounded region, entity, and quest cursors", () => {
  const load = extractFunction(expand, "loadFocusPage");
  const more = extractFunction(expand, "loadMoreFocus");

  assert.match(expand, /type FocusArchiveKind = "region" \| "entity" \| "quest"/);
  assert.match(load, /new URLSearchParams\(\{ kind, limit: String\(FOCUS_PAGE_SIZE\) \}\)/);
  assert.match(load, /params\.set\("cursor", cursor\)/);
  assert.match(load, /sequence !== focusLoadSequence\[kind\]/);
  assert.match(load, /body\.next_cursor/);
  assert.match(load, /error instanceof ApiError && error\.status === 409/);
  assert.match(load, /await loadFocusPage\(kind\)/);
  assert.match(more, /await loadFocusPage\(kind, cursor, true\)/);
  assert.match(expand, /Promise\.all\(FOCUS_SOURCES\.map/);
  assert.match(expand, /<optgroup v-for="\(options, group\) in focusGroups"/);
  assert.match(expand, /@click="loadMoreFocus\(source\.key\)"/);
  assert.doesNotMatch(expand, /`\/projects\/\$\{currentProject\(\)\}\/archive`/);
});

test("command palette uses one focused dialogue page and only offers supported refactors", () => {
  const navigation = extractFunction(palette, "navigationInventory");
  const jump = extractFunction(palette, "jump");
  const renameKinds = palette.match(/const RENAMEABLE_KINDS = new Set\(\[[\s\S]*?\]\);/)?.[0] ?? "";

  assert.match(
    navigation,
    /new URLSearchParams\(\{ kind: "dialogue", focus: dialogueRef, limit: "1" \}\)/,
  );
  assert.match(navigation, /body\.items/);
  assert.match(navigation, /body\.dialogue_tree_payloads/);
  assert.match(jump, /navigationInventory\(hit\.ref\)/);
  assert.match(palette, /v-if="canRename\(hit\)"/);
  for (const kind of [
    "entity",
    "quest",
    "region",
    "poi",
    "term",
    "dialogue",
    "dialogue_tree",
    "localized_text",
  ]) {
    assert.match(renameKinds, new RegExp(`"${kind}"`));
  }
  for (const kind of ["relation", "style_guide", "quest_event_ref", "style_rubric"]) {
    assert.doesNotMatch(renameKinds, new RegExp(`"${kind}"`));
  }
  assert.doesNotMatch(navigation, /\/archive`/);
});

test("genesis reads only one reference row when it needs the library count", () => {
  assert.match(genesis, /\/references\?limit=1/);
  assert.doesNotMatch(genesis, /`\/projects\/\$\{currentProject\(\)\}\/references`/);
});
