import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "typescript";

const source = await readFile(
  new URL("../src/pages/ReviewPage.vue", import.meta.url),
  "utf8",
);

function extractFunction(name) {
  const match = new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\(`).exec(source);
  assert.ok(match, `ReviewPage must define ${name}`);
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

let harnessSequence = 0;

async function loadHarness(runtime) {
  globalThis.__owReviewCursorRuntime = runtime;
  harnessSequence += 1;
  const typescript = `
    const runtime = globalThis.__owReviewCursorRuntime;
    const ref = (value) => ({ value });
    const items = ref([{ id: "old-first" }]);
    const queueLoading = ref(false);
    const queueError = ref("");
    const queueTotal = ref(2);
    const nextQueueCursor = ref("old-cursor");
    const loadingMore = ref(false);
    let queueLoadSequence = 0;
    const currentProject = () => "demo";
    const apiGet = (path) => runtime.apiGet(path);
    const humanizeError = (error) => String(error);
    const notifyError = (error) => runtime.notifications.push(error);
    const loadCalibration = async () => { runtime.calibrationLoads += 1; };
    const loadLessons = async () => { runtime.lessonLoads += 1; };

    ${extractFunction("invalidateQueuePaging")}
    ${extractFunction("refresh")}
    ${extractFunction("loadMoreItems")}

    export {
      invalidateQueuePaging,
      refresh,
      loadMoreItems,
      items,
      queueLoading,
      queueError,
      queueTotal,
      nextQueueCursor,
      loadingMore,
    };
    // isolated harness ${harnessSequence}
  `;
  const javascript = ts.transpileModule(typescript, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  return import(`data:text/javascript,${encodeURIComponent(javascript)}`);
}

function runtimeWith(...responses) {
  return {
    responses: [...responses],
    requests: [],
    notifications: [],
    calibrationLoads: 0,
    lessonLoads: 0,
    apiGet(path) {
      this.requests.push(path);
      const response = this.responses.shift();
      assert.ok(response, `unexpected request: ${path}`);
      return response.promise;
    },
  };
}

test("a refresh snapshot wins when an older cursor page resolves last", async () => {
  const oldPage = deferred();
  const freshPage = deferred();
  const runtime = runtimeWith(oldPage, freshPage);
  const review = await loadHarness(runtime);

  const loadingOldPage = review.loadMoreItems();
  const refreshing = review.refresh();

  freshPage.resolve({
    items: [{ id: "fresh-first" }],
    count: 1,
    next_cursor: "fresh-cursor",
  });
  await refreshing;
  oldPage.resolve({
    items: [{ id: "stale-second" }],
    count: 2,
    next_cursor: null,
  });
  await loadingOldPage;

  assert.deepEqual(review.items.value.map((item) => item.id), ["fresh-first"]);
  assert.equal(review.queueTotal.value, 1);
  assert.equal(review.nextQueueCursor.value, "fresh-cursor");
  assert.equal(review.queueLoading.value, false);
  assert.equal(review.loadingMore.value, false);
  assert.deepEqual(runtime.notifications, []);
  assert.match(runtime.requests[0], /cursor=old-cursor/);
  assert.match(runtime.requests[1], /review_items\?limit=50$/);
});

test("a decision invalidation prevents its in-flight old cursor page from appending", async () => {
  const oldPage = deferred();
  const runtime = runtimeWith(oldPage);
  const review = await loadHarness(runtime);

  const loadingOldPage = review.loadMoreItems();
  review.invalidateQueuePaging();
  oldPage.resolve({
    items: [{ id: "stale-second" }],
    count: 2,
    next_cursor: null,
  });
  await loadingOldPage;

  assert.deepEqual(review.items.value.map((item) => item.id), ["old-first"]);
  assert.equal(review.nextQueueCursor.value, null);
  assert.equal(review.loadingMore.value, false);
  assert.match(extractFunction("decide"), /invalidateQueuePaging\(\)/);
});
