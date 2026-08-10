import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const workspaces = await readFile(
  new URL("../src/pages/WorkspacesPage.vue", import.meta.url),
  "utf8",
);
const app = await readFile(new URL("../src/App.vue", import.meta.url), "utf8");
const exportPage = await readFile(
  new URL("../src/pages/ExportPage.vue", import.meta.url),
  "utf8",
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

test("workspace manager follows bounded opaque cursors with stale-response fencing", () => {
  const path = extractFunction(workspaces, "workspaceListPath");
  const load = extractFunction(workspaces, "loadWorkspaces");
  const more = extractFunction(workspaces, "loadMoreWorkspaces");

  assert.match(workspaces, /const PAGE_SIZE = 30/);
  assert.match(path, /limit: String\(PAGE_SIZE\)/);
  assert.match(path, /query\.set\("focus", active\.value\)/);
  assert.match(path, /query\.set\("cursor", cursor\)/);
  assert.match(load, /const sequence = \+\+listLoadSequence/);
  assert.ok(
    (load.match(/sequence !== listLoadSequence/g) ?? []).length >= 2,
    "success and error paths must both ignore stale responses",
  );
  assert.match(load, /append && e instanceof ApiError && e\.status === 409/);
  assert.match(load, /await loadWorkspaces\(\)/);
  assert.match(load, /new Map\(rows\.map/);
  assert.match(more, /nextCursor\.value/);
  assert.match(more, /loadWorkspaces\(nextCursor\.value, true\)/);
  assert.match(workspaces, /加载更多（剩余/);
});

test("global shell and export probe never request the unbounded workspace catalog", () => {
  const shell = extractFunction(app, "loadLocalShell");
  assert.match(shell, /new URLSearchParams\(\{ limit: "30" \}\)/);
  assert.match(shell, /query\.set\("focus", focus\)/);
  assert.match(shell, /`\/workspaces\?\$\{query\.toString\(\)\}`/);

  assert.match(exportPage, /new URLSearchParams\(\{ limit: "1", focus: project \}\)/);
  assert.match(exportPage, /`\/workspaces\?\$\{query\.toString\(\)\}`/);
});
