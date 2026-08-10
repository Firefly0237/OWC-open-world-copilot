import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "typescript";

const source = await readFile(new URL("../src/pages/TimelinePage.vue", import.meta.url), "utf8");

function extractFunction(name) {
  const match = new RegExp(`(?:async\\s+)?function\\s+${name}(?:<[^>]+>)?`).exec(source);
  assert.ok(match, `TimelinePage must define ${name}`);
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

const queueFunction = extractFunction("queueLockRequest");
const queueHarness = ts.transpileModule(
  `const lockRequestChains = new Map<string, Promise<void>>();\n${queueFunction}\nexport { queueLockRequest };`,
  { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } },
).outputText;
const { queueLockRequest } = await import(`data:text/javascript,${encodeURIComponent(queueHarness)}`);

test("timeline lock requests serialize per object without blocking a different object", async () => {
  let releaseA;
  let markAStarted;
  const aGate = new Promise((resolve) => { releaseA = resolve; });
  const aStarted = new Promise((resolve) => { markAStarted = resolve; });
  const order = [];

  const firstA = queueLockRequest("quest:a", async () => {
    order.push("a1:start");
    markAStarted();
    await aGate;
    order.push("a1:end");
  });
  await aStarted;
  const secondA = queueLockRequest("quest:a", async () => { order.push("a2"); });
  const firstB = queueLockRequest("quest:b", async () => { order.push("b1"); });
  await firstB;
  assert.deepEqual(order, ["a1:start", "b1"]);

  releaseA();
  await Promise.all([firstA, secondA]);
  assert.deepEqual(order, ["a1:start", "b1", "a1:end", "a2"]);
});

test("an in-flight heartbeat is followed by release and cannot renew afterwards", async () => {
  const ref = "quest:heartbeat";
  const desired = new Map([[ref, true]]);
  let held = true;
  let releaseHeartbeat;
  let markHeartbeatStarted;
  const heartbeatGate = new Promise((resolve) => { releaseHeartbeat = resolve; });
  const heartbeatStarted = new Promise((resolve) => { markHeartbeatStarted = resolve; });
  const posts = [];

  const heartbeat = () => queueLockRequest(ref, async () => {
    if (desired.get(ref) !== true) return;
    posts.push("heartbeat");
    markHeartbeatStarted();
    await heartbeatGate;
    held = true;
  });
  const setLock = (release) => {
    desired.set(ref, !release);
    return queueLockRequest(ref, async () => {
      if (!release && desired.get(ref) !== true) return false;
      posts.push(release ? "release" : "acquire");
      held = !release;
      if (release && desired.get(ref) === false) desired.delete(ref);
      return true;
    });
  };

  const inFlightHeartbeat = heartbeat();
  await heartbeatStarted;
  const release = setLock(true);
  const heartbeatQueuedAfterReleaseIntent = heartbeat();
  releaseHeartbeat();
  await Promise.all([inFlightHeartbeat, release, heartbeatQueuedAfterReleaseIntent]);

  assert.deepEqual(posts, ["heartbeat", "release"]);
  assert.equal(held, false);
});

test("timeline source retains selection, last-call, and unload race guards", () => {
  const lockBody = extractFunction("setEditLock");
  assert.ok(
    lockBody.indexOf("lockDesired.set(objectRef, !release)")
      < lockBody.indexOf("queueLockRequest(objectRef"),
    "lock intent must be recorded before the request is queued",
  );
  assert.match(lockBody, /if \(!release && lockDesired\.get\(objectRef\) !== true\) return false/);

  const heartbeatBody = extractFunction("syncLockHeartbeat");
  assert.match(heartbeatBody, /if \(lockDesired\.get\(objectRef\) === false\) return/);
  assert.ok(
    (heartbeatBody.match(/lockDesired\.get\(objectRef\) !== true/g) ?? []).length >= 2,
    "heartbeat must check release intent both before queueing and before posting",
  );

  const collabBody = extractFunction("loadCollab");
  assert.match(source, /const COLLAB_PAGE_SIZE = 20/);
  assert.match(collabBody, /limit=\$\{COLLAB_PAGE_SIZE\}/);
  assert.match(collabBody, /cursor=\$\{encodeURIComponent\(cursor\)\}/);
  assert.match(collabBody, /body\.total/);
  assert.match(collabBody, /body\.next_cursor/);
  assert.match(collabBody, /append && e instanceof ApiError && e\.status === 409/);
  assert.match(collabBody, /await loadCollab\(objectRef\)/);
  assert.ok(
    (collabBody.match(/selected\.value === objectRef && collabRequestSerial === requestSerial/g) ?? [])
      .length >= 2,
      "stale collab successes and failures must both be selection guarded",
  );
  assert.match(extractFunction("ensureOwnLock"), /collab\?object_ref=\$\{encodeURIComponent\(objectRef\)\}&limit=1/);

  const commentBody = extractFunction("postComment");
  assert.match(commentBody, /const objectRef = selected\.value/);
  assert.match(commentBody, /if \(selected\.value === objectRef\)/);
  assert.match(commentBody, /await loadCollab\(objectRef\)/);
  assert.match(source, /加载更早评论/);
  assert.match(source, /@click="loadCollab\(selected, true\)"/);

  for (const [name, serial, minimum] of [
    ["runSim", "simRequestSerial", 2],
    ["draftLogic", "draftRequestSerial", 3],
  ]) {
    const body = extractFunction(name);
    const guard = new RegExp(
      `selected\\.value === objectRef\\s*&&\\s*selectionEpoch === epoch\\s*&&\\s*${serial} === requestSerial`,
      "g",
    );
    assert.ok(
      (body.match(guard) ?? []).length >= minimum,
      `${name} must ignore stale results, errors, and completion state`,
    );
  }

  const unmount = source.slice(source.indexOf("onBeforeUnmount(() =>"), source.indexOf("</script>"));
  assert.match(unmount, /desired === true \|\| \(desired !== false && lockHeldByMe\.value\)/);
  assert.ok(
    unmount.indexOf('selected.value = ""') < unmount.indexOf("setEditLock(true, objectRef, true)"),
    "unmount must invalidate selection before its queued release can refresh collab state",
  );
});
