import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "typescript";

const [timelineSource, dialoguesSource] = await Promise.all([
  readFile(new URL("../src/pages/TimelinePage.vue", import.meta.url), "utf8"),
  readFile(new URL("../src/pages/DialoguesPage.vue", import.meta.url), "utf8"),
]);

function extractFunction(source, name) {
  const match = new RegExp(`(?:async\\s+)?function\\s+${name}(?:<[^>]+>)?`).exec(source);
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

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

test("timeline quest editor hydrates and explicitly saves giver and location references", () => {
  const hydrate = extractFunction(timelineSource, "hydrateQuest");
  const save = extractFunction(timelineSource, "saveEdit");

  assert.match(timelineSource, /interface TimelineEditDraft[\s\S]*giverNpc: string;[\s\S]*location: string;/);
  assert.match(hydrate, /giver_npc\?: string \| null/);
  assert.match(hydrate, /location\?: string \| null/);
  assert.match(hydrate, /edit\.value\.giverNpc = body\.quest\.giver_npc \?\? ""/);
  assert.match(hydrate, /edit\.value\.location = body\.quest\.location \?\? ""/);
  assert.match(save, /giverNpc: edit\.value\.giverNpc\.trim\(\)/);
  assert.match(save, /location: edit\.value\.location\.trim\(\)/);
  assert.match(save, /giver_npc: draftSnapshot\.giverNpc/);
  assert.match(save, /location: draftSnapshot\.location/);
  assert.match(timelineSource, /v-model="edit\.giverNpc"/);
  assert.match(timelineSource, /v-model="edit\.location"/);
  assert.match(timelineSource, /留空保存可清除/);
});

let titleHarnessSequence = 0;
async function loadTitleHarness(runtime) {
  globalThis.__owDialogueTitleRuntime = runtime;
  titleHarnessSequence += 1;
  const typescript = `
    const runtime = globalThis.__owDialogueTitleRuntime;
    const ref = (value) => ({ value });
    const selected = ref("tree-a");
    const flow = ref({ tree_id: "tree-a", title: "Old title", nodes: [], edges: [] });
    const treeTitleDraft = ref("  New title  ");
    const saving = ref(false);
    const loading = ref(false);
    const error = ref("");
    const flash = ref("");
    const trees = ref([{ id: "tree-a", title: "Old title", participants: [], node_count: 1 }]);
    const localNodes = ref({ n1: { id: "n1", text: "unsaved draft" } });
    let openEpoch = 7;
    const proj = () => "demo";
    const apiPatch = (path, body) => runtime.apiPatch(path, body);
    const humanizeError = (caught) => String(caught);
    const flashOk = (message) => { flash.value = message; error.value = ""; };

    ${extractFunction(dialoguesSource, "saveTreeTitle")}

    export { saveTreeTitle, flow, treeTitleDraft, saving, error, flash, trees, localNodes };
    // isolated title harness ${titleHarnessSequence}
  `;
  const javascript = ts.transpileModule(typescript, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  return import(`data:text/javascript,${encodeURIComponent(javascript)}`);
}

test("dialogue rename PATCH sends title alone and preserves local node state", async () => {
  const requests = [];
  const dialogue = await loadTitleHarness({
    apiPatch(path, body) {
      requests.push({ path, body });
      return Promise.resolve({ tree: { title: "New title" } });
    },
  });
  const nodesBefore = JSON.stringify(dialogue.localNodes.value);

  await dialogue.saveTreeTitle();

  assert.deepEqual(requests, [
    {
      path: "/projects/demo/dialogue_trees/tree-a",
      body: { title: "New title" },
    },
  ]);
  assert.equal(dialogue.flow.value.title, "New title");
  assert.equal(dialogue.treeTitleDraft.value, "New title");
  assert.equal(dialogue.trees.value[0].title, "New title");
  assert.equal(JSON.stringify(dialogue.localNodes.value), nodesBefore);
  assert.equal(dialogue.flash.value, "标题已保存。");
  assert.equal(dialogue.saving.value, false);
});

let pagingHarnessSequence = 0;
async function loadPagingHarness(runtime) {
  globalThis.__owDialoguePagingRuntime = runtime;
  pagingHarnessSequence += 1;
  const typescript = `
    interface TreeSummary {
      id: string;
      title: string;
      participants: string[];
      node_count: number;
    }
    const runtime = globalThis.__owDialoguePagingRuntime;
    const ref = (value) => ({ value });
    class ApiError extends Error {
      status: number;
      constructor(status, detail) { super(detail); this.status = status; }
    }
    const trees = ref([{ id: "old", title: "Old", participants: [], node_count: 1 }]);
    const treeTotal = ref(2);
    const nextTreeCursor = ref("stale cursor");
    const treesLoadingMore = ref(false);
    const treeListFocus = ref("tree-focus");
    const selected = ref("tree-focus");
    const saving = ref(false);
    const error = ref("");
    let treeListLoadSequence = 0;
    const proj = () => "demo";
    const apiGet = (path) => runtime.apiGet(path);
    const humanizeError = (caught) => String(caught);

    ${extractFunction(dialoguesSource, "fetchTreeList")}
    ${extractFunction(dialoguesSource, "loadMoreTrees")}

    export {
      ApiError,
      fetchTreeList,
      loadMoreTrees,
      trees,
      treeTotal,
      nextTreeCursor,
      treesLoadingMore,
      error,
    };
    // isolated paging harness ${pagingHarnessSequence}
  `;
  const javascript = ts.transpileModule(typescript, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  return import(`data:text/javascript,${encodeURIComponent(javascript)}`);
}

for (const status of [409, 422]) {
  test(`dialogue continuation HTTP ${status} clears the cursor and reloads page one`, async () => {
    const requests = [];
    const responses = [];
    const runtime = {
      apiGet(path) {
        requests.push(path);
        const response = responses.shift();
        assert.ok(response, `unexpected GET: ${path}`);
        return response;
      },
    };
    const dialogue = await loadPagingHarness(runtime);
    responses.push(
      Promise.reject(new dialogue.ApiError(status, "cursor rejected")),
      Promise.resolve({
        dialogues: {
          trees: [{ id: "fresh", title: "Fresh", participants: [], node_count: 2 }],
        },
        total: 1,
        next_cursor: null,
      }),
    );

    await dialogue.loadMoreTrees();

    assert.match(requests[0], /limit=24&cursor=stale\+cursor&focus=tree-focus$/);
    assert.match(requests[1], /limit=24&focus=tree-focus$/);
    assert.deepEqual(dialogue.trees.value.map((tree) => tree.id), ["fresh"]);
    assert.equal(dialogue.treeTotal.value, 1);
    assert.equal(dialogue.nextTreeCursor.value, null);
    assert.equal(dialogue.error.value, "");
    assert.equal(dialogue.treesLoadingMore.value, false);
  });
}

test("a newer dialogue list refresh fences an older continuation response", async () => {
  const oldContinuation = deferred();
  const freshFirstPage = deferred();
  const responses = [oldContinuation.promise, freshFirstPage.promise];
  const runtime = {
    apiGet(path) {
      const response = responses.shift();
      assert.ok(response, `unexpected GET: ${path}`);
      return response;
    },
  };
  const dialogue = await loadPagingHarness(runtime);

  const loadingMore = dialogue.loadMoreTrees();
  const refreshing = dialogue.fetchTreeList(null, false, "fresh-focus");
  freshFirstPage.resolve({
    dialogues: {
      trees: [{ id: "fresh", title: "Fresh", participants: [], node_count: 1 }],
    },
    total: 1,
    next_cursor: null,
  });
  await refreshing;
  oldContinuation.resolve({
    dialogues: {
      trees: [{ id: "stale", title: "Stale", participants: [], node_count: 1 }],
    },
    total: 3,
    next_cursor: "wrong-next",
  });
  await loadingMore;

  assert.deepEqual(dialogue.trees.value.map((tree) => tree.id), ["fresh"]);
  assert.equal(dialogue.treeTotal.value, 1);
  assert.equal(dialogue.nextTreeCursor.value, null);
  assert.equal(dialogue.error.value, "");
});
