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
  globalThis.__owLessonCursorRuntime = runtime;
  harnessSequence += 1;
  const typescript = `
    const runtime = globalThis.__owLessonCursorRuntime;
    const ref = (value) => ({ value });
    const lessons = ref([{ id: "old-first" }]);
    const lessonTotal = ref(2);
    const nextLessonCursor = ref("old-cursor");
    const lessonsLoadingMore = ref(false);
    let lessonLoadSequence = 0;
    const currentProject = () => "demo";
    const apiGet = (path) => runtime.apiGet(path);
    const notifyError = (error) => runtime.notifications.push(String(error));

    ${extractFunction("loadLessons")}
    ${extractFunction("loadMoreLessons")}

    export {
      loadLessons,
      loadMoreLessons,
      lessons,
      lessonTotal,
      nextLessonCursor,
      lessonsLoadingMore,
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
    apiGet(path) {
      this.requests.push(path);
      const response = this.responses.shift();
      assert.ok(response, `unexpected request: ${path}`);
      return response.promise;
    },
  };
}

test("a fresh lesson snapshot wins when an older cursor page resolves last", async () => {
  const oldPage = deferred();
  const freshPage = deferred();
  const runtime = runtimeWith(oldPage, freshPage);
  const review = await loadHarness(runtime);

  const loadingOldPage = review.loadMoreLessons();
  const refreshing = review.loadLessons();
  freshPage.resolve({
    lessons: [{ id: "fresh-first" }],
    total: 1,
    next_cursor: null,
  });
  await refreshing;
  oldPage.resolve({
    lessons: [{ id: "stale-second" }],
    total: 2,
    next_cursor: null,
  });
  await loadingOldPage;

  assert.deepEqual(review.lessons.value.map((lesson) => lesson.id), ["fresh-first"]);
  assert.equal(review.lessonTotal.value, 1);
  assert.equal(review.nextLessonCursor.value, null);
  assert.equal(review.lessonsLoadingMore.value, false);
  assert.deepEqual(runtime.notifications, []);
  assert.match(runtime.requests[0], /cursor=old-cursor/);
  assert.match(runtime.requests[1], /lessons\?limit=30$/);
});

test("a stale cursor error reports once and reloads lesson page one", async () => {
  const stalePage = deferred();
  const freshPage = deferred();
  const runtime = runtimeWith(stalePage, freshPage);
  const review = await loadHarness(runtime);

  const loading = review.loadMoreLessons();
  freshPage.resolve({
    lessons: [{ id: "current-first" }],
    total: 1,
    next_cursor: null,
  });
  stalePage.reject(new Error("409 snapshot stale"));
  await loading;

  assert.deepEqual(review.lessons.value.map((lesson) => lesson.id), ["current-first"]);
  assert.equal(review.lessonTotal.value, 1);
  assert.equal(review.nextLessonCursor.value, null);
  assert.deepEqual(runtime.notifications, ["Error: 409 snapshot stale"]);
  assert.match(runtime.requests[1], /lessons\?limit=30$/);
});
