import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "typescript";

const source = await readFile(
  new URL("../src/pages/TimelinePage.vue", import.meta.url),
  "utf8",
);

function extractFunction(name) {
  const match = new RegExp(`(?:async\\s+)?function\\s+${name}(?:<[^>]+>)?`).exec(source);
  assert.ok(match, `TimelinePage must define ${name}`);
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
  globalThis.__owTimelineCollabRuntime = runtime;
  harnessSequence += 1;
  const typescript = `
    const runtime = globalThis.__owTimelineCollabRuntime;
    const ref = (value) => ({ value });
    class ApiError extends Error {
      constructor(status, detail) { super(detail); this.status = status; }
    }
    const COLLAB_PAGE_SIZE = 20;
    const selected = ref("quest:a");
    const collab = ref({ assignee: null, comments: [{ id: "first" }], lock: null });
    const collabLoading = ref(false);
    const collabLoadingMore = ref(false);
    const collabTotal = ref(2);
    const collabNextCursor = ref("old cursor");
    const collabSnapshotEtag = ref("old-etag");
    const draftComment = ref("");
    const commentBusy = ref(false);
    let collabRequestSerial = 0;
    const proj = () => "demo";
    const meName = () => "alice";
    const apiGet = (path) => runtime.apiGet(path);
    const apiPost = (path, body) => runtime.apiPost(path, body);
    const notifyError = (error) => runtime.notifications.push(String(error));

    ${extractFunction("resetCollabView")}
    ${extractFunction("loadCollab")}
    ${extractFunction("postComment")}

    export {
      ApiError,
      selected,
      resetCollabView,
      loadCollab,
      postComment,
      collab,
      collabLoading,
      collabLoadingMore,
      collabTotal,
      collabNextCursor,
      collabSnapshotEtag,
      draftComment,
      commentBusy,
    };
    // isolated harness ${harnessSequence}
  `;
  const javascript = ts.transpileModule(typescript, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  return import(`data:text/javascript,${encodeURIComponent(javascript)}`);
}

function runtimeWith({ gets = [], posts = [] } = {}) {
  return {
    gets: [...gets],
    posts: [...posts],
    getRequests: [],
    postRequests: [],
    notifications: [],
    apiGet(path) {
      this.getRequests.push(path);
      const response = this.gets.shift();
      assert.ok(response, `unexpected GET: ${path}`);
      return response.promise;
    },
    apiPost(path, body) {
      this.postRequests.push({ path, body });
      const response = this.posts.shift();
      assert.ok(response, `unexpected POST: ${path}`);
      return response.promise;
    },
  };
}

test("timeline refresh fences an older collaboration continuation", async () => {
  const oldPage = deferred();
  const freshPage = deferred();
  const runtime = runtimeWith({ gets: [oldPage, freshPage] });
  const timeline = await loadHarness(runtime);

  const loadingOldPage = timeline.loadCollab("quest:a", true);
  const refreshing = timeline.loadCollab("quest:a");
  freshPage.resolve({
    assignment: { assignee: "fresh-owner" },
    comments: [{ id: "fresh" }],
    lock: { holder: "fresh-lock" },
    total: 1,
    next_cursor: null,
    snapshot_etag: "fresh-etag",
  });
  await refreshing;
  oldPage.resolve({
    assignment: { assignee: "stale-owner" },
    comments: [{ id: "stale" }],
    lock: { holder: "stale-lock" },
    total: 2,
    next_cursor: null,
    snapshot_etag: "stale-etag",
  });
  await loadingOldPage;

  assert.deepEqual(timeline.collab.value.comments.map((comment) => comment.id), ["fresh"]);
  assert.equal(timeline.collab.value.assignee, "fresh-owner");
  assert.equal(timeline.collab.value.lock.holder, "fresh-lock");
  assert.equal(timeline.collabTotal.value, 1);
  assert.equal(timeline.collabSnapshotEtag.value, "fresh-etag");
  assert.equal(timeline.collabLoading.value, false);
  assert.equal(timeline.collabLoadingMore.value, false);
  assert.match(runtime.getRequests[0], /limit=20&cursor=old%20cursor$/);
  assert.match(runtime.getRequests[1], /limit=20$/);
});

test("timeline stale collaboration cursor restarts at page one", async () => {
  const stalePage = deferred();
  const freshPage = deferred();
  const runtime = runtimeWith({ gets: [stalePage, freshPage] });
  const timeline = await loadHarness(runtime);

  const loading = timeline.loadCollab("quest:a", true);
  stalePage.reject(new timeline.ApiError(409, "snapshot stale"));
  freshPage.resolve({
    assignment: null,
    comments: [{ id: "newest" }],
    lock: null,
    total: 1,
    next_cursor: null,
    snapshot_etag: "new-etag",
  });
  await loading;

  assert.deepEqual(timeline.collab.value.comments.map((comment) => comment.id), ["newest"]);
  assert.equal(timeline.collabNextCursor.value, null);
  assert.deepEqual(runtime.notifications, []);
  assert.match(runtime.getRequests[1], /collab\?object_ref=quest%3Aa&limit=20$/);
});

test("switching timeline objects fences an in-flight collaboration response", async () => {
  const oldObject = deferred();
  const runtime = runtimeWith({ gets: [oldObject] });
  const timeline = await loadHarness(runtime);

  const loading = timeline.loadCollab("quest:a");
  timeline.selected.value = "quest:b";
  timeline.resetCollabView();
  oldObject.resolve({
    assignment: { assignee: "wrong-object" },
    comments: [{ id: "stale" }],
    lock: { holder: "wrong-object" },
    total: 1,
    next_cursor: null,
    snapshot_etag: "stale",
  });
  await loading;

  assert.deepEqual(timeline.collab.value, { assignee: null, comments: [], lock: null });
  assert.equal(timeline.collabTotal.value, 0);
  assert.deepEqual(runtime.notifications, []);
});

test("timeline comment post refreshes page one only while its quest remains selected", async () => {
  const posted = deferred();
  const refreshed = deferred();
  const runtime = runtimeWith({ posts: [posted], gets: [refreshed] });
  const timeline = await loadHarness(runtime);
  timeline.draftComment.value = "new comment";

  const submitting = timeline.postComment();
  posted.resolve({});
  refreshed.resolve({
    assignment: { assignee: "alice" },
    comments: [{ id: "posted", body: "new comment" }],
    lock: null,
    total: 1,
    next_cursor: null,
    snapshot_etag: "after-post",
  });
  await submitting;

  assert.deepEqual(runtime.postRequests[0], {
    path: "/projects/demo/collab/comments",
    body: { object_ref: "quest:a", author: "alice", body: "new comment" },
  });
  assert.match(runtime.getRequests[0], /object_ref=quest%3Aa&limit=20$/);
  assert.deepEqual(timeline.collab.value.comments.map((comment) => comment.id), ["posted"]);
  assert.equal(timeline.draftComment.value, "");
  assert.equal(timeline.commentBusy.value, false);
});
