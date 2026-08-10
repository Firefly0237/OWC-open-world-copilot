import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "typescript";

const source = await readFile(
  new URL("../src/pages/ArchivePage.vue", import.meta.url),
  "utf8",
);

function extractFunction(name) {
  const match = new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\(`).exec(source);
  assert.ok(match, `ArchivePage must define ${name}`);
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
  globalThis.__owArchiveCollabRuntime = runtime;
  harnessSequence += 1;
  const typescript = `
    const runtime = globalThis.__owArchiveCollabRuntime;
    const ref = (value) => ({ value });
    class ApiError extends Error {
      constructor(status, detail) { super(detail); this.status = status; }
    }
    const COLLAB_PAGE_SIZE = 20;
    const activeObjectRef = ref("quest:a");
    const collab = ref({ assignment: null, comments: [{ id: "first" }], lock: null });
    const collabLoading = ref(false);
    const collabLoadingMore = ref(false);
    const collabTotal = ref(2);
    const collabNextCursor = ref("old cursor");
    const collabSnapshotEtag = ref("old-etag");
    const draftComment = ref("");
    let collabRequestSerial = 0;
    const currentProject = () => "demo";
    const apiGet = (path) => runtime.apiGet(path);
    const notifyError = (error) => runtime.notifications.push(String(error));

    ${extractFunction("clearCollabView")}
    ${extractFunction("loadCollab")}

    export {
      ApiError,
      activeObjectRef,
      clearCollabView,
      loadCollab,
      collab,
      collabLoading,
      collabLoadingMore,
      collabTotal,
      collabNextCursor,
      collabSnapshotEtag,
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

test("a fresh collaboration page wins when an older continuation resolves last", async () => {
  const oldPage = deferred();
  const freshPage = deferred();
  const runtime = runtimeWith(oldPage, freshPage);
  const archive = await loadHarness(runtime);

  const loadingOldPage = archive.loadCollab("quest:a", true);
  const refreshing = archive.loadCollab("quest:a");
  freshPage.resolve({
    assignment: { assignee: "fresh-owner" },
    comments: [{ id: "fresh" }],
    lock: null,
    total: 1,
    next_cursor: null,
    snapshot_etag: "fresh-etag",
  });
  await refreshing;
  oldPage.resolve({
    assignment: { assignee: "old-owner" },
    comments: [{ id: "stale" }],
    lock: null,
    total: 2,
    next_cursor: null,
    snapshot_etag: "old-etag",
  });
  await loadingOldPage;

  assert.deepEqual(archive.collab.value.comments.map((comment) => comment.id), ["fresh"]);
  assert.equal(archive.collab.value.assignment.assignee, "fresh-owner");
  assert.equal(archive.collabTotal.value, 1);
  assert.equal(archive.collabSnapshotEtag.value, "fresh-etag");
  assert.equal(archive.collabLoading.value, false);
  assert.equal(archive.collabLoadingMore.value, false);
  assert.deepEqual(runtime.notifications, []);
  assert.match(runtime.requests[0], /limit=20&cursor=old%20cursor$/);
  assert.match(runtime.requests[1], /limit=20$/);
});

test("a stale collaboration cursor silently restarts at the newest page", async () => {
  const stalePage = deferred();
  const freshPage = deferred();
  const runtime = runtimeWith(stalePage, freshPage);
  const archive = await loadHarness(runtime);

  const loading = archive.loadCollab("quest:a", true);
  stalePage.reject(new archive.ApiError(409, "snapshot stale"));
  freshPage.resolve({
    assignment: null,
    comments: [{ id: "new-comment" }],
    lock: null,
    total: 1,
    next_cursor: null,
    snapshot_etag: "new-etag",
  });
  await loading;

  assert.deepEqual(archive.collab.value.comments.map((comment) => comment.id), ["new-comment"]);
  assert.equal(archive.collabTotal.value, 1);
  assert.equal(archive.collabNextCursor.value, null);
  assert.deepEqual(runtime.notifications, []);
  assert.match(runtime.requests[1], /collab\?object_ref=quest%3Aa&limit=20$/);
});

test("switching archive objects fences an in-flight collaboration response", async () => {
  const oldObject = deferred();
  const runtime = runtimeWith(oldObject);
  const archive = await loadHarness(runtime);

  const loading = archive.loadCollab("quest:a");
  archive.activeObjectRef.value = "entity:b";
  archive.clearCollabView();
  oldObject.resolve({
    assignment: { assignee: "wrong-object" },
    comments: [{ id: "stale" }],
    lock: { holder: "wrong-object" },
    total: 1,
    next_cursor: null,
    snapshot_etag: "stale",
  });
  await loading;

  assert.deepEqual(archive.collab.value, { assignment: null, comments: [], lock: null });
  assert.equal(archive.collabTotal.value, 0);
  assert.equal(archive.collabLoading.value, false);
  assert.deepEqual(runtime.notifications, []);
});

test("posting a comment refreshes page one for the same object", () => {
  const postComment = extractFunction("postComment");
  assert.match(postComment, /await apiPost\(`\/projects\/\$\{currentProject\(\)\}\/collab\/comments`/);
  assert.match(postComment, /if \(activeObjectRef\.value === objectRef\)/);
  assert.match(postComment, /await loadCollab\(objectRef\)/);
});
