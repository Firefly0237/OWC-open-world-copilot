import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "typescript";

const graphPageSource = await readFile(new URL("../src/pages/GraphPage.vue", import.meta.url), "utf8");
const queueScript = graphPageSource.match(/<script lang="ts">\s*([\s\S]*?)<\/script>/)?.[1];
assert.ok(queueScript, "GraphPage must expose its position queue in the normal TypeScript block");
const queueJavaScript = ts.transpileModule(queueScript, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
}).outputText;
const queueModule = await import(`data:text/javascript,${encodeURIComponent(queueJavaScript)}`);
const { createGraphPositionQueue } = queueModule;

test("failed graph positions remain pending and can be retried", async () => {
  let shouldFail = true;
  const calls = [];
  const queue = createGraphPositionQueue({
    async save(ref, position) {
      calls.push({ ref, ...position });
      if (shouldFail) throw new Error("offline");
    },
  });

  assert.equal(await queue.enqueue("entity:harbor", { x: 1, y: 2, z: 3 }), false);
  assert.deepEqual(queue.snapshot(), [["entity:harbor", { x: 1, y: 2, z: 3 }]]);
  assert.deepEqual(queue.state(), { pending: 1, saving: false, failed: true });

  shouldFail = false;
  assert.equal(await queue.retry(), true);
  assert.deepEqual(calls, [
    { ref: "entity:harbor", x: 1, y: 2, z: 3 },
    { ref: "entity:harbor", x: 1, y: 2, z: 3 },
  ]);
  assert.deepEqual(queue.snapshot(), []);
  assert.deepEqual(queue.state(), { pending: 0, saving: false, failed: false });
});

test("moves made during an in-flight save stay single-flight and persist the latest position", async () => {
  let releaseFirst;
  let markFirstStarted;
  const firstGate = new Promise((resolve) => { releaseFirst = resolve; });
  const firstStarted = new Promise((resolve) => { markFirstStarted = resolve; });
  const calls = [];
  let concurrent = 0;
  let maxConcurrent = 0;
  const queue = createGraphPositionQueue({
    async save(ref, position) {
      concurrent += 1;
      maxConcurrent = Math.max(maxConcurrent, concurrent);
      calls.push({ ref, ...position });
      try {
        if (calls.length === 1) {
          markFirstStarted();
          await firstGate;
        }
      } finally {
        concurrent -= 1;
      }
    },
  });

  const firstFlush = queue.enqueue("entity:harbor", { x: 1, y: 2, z: 3 });
  await firstStarted;
  const joinedFlush = queue.enqueue("entity:harbor", { x: 7, y: 8, z: 9 });
  assert.equal(joinedFlush, firstFlush, "concurrent enqueue should join the active flush");
  releaseFirst();
  assert.equal(await firstFlush, true);

  assert.equal(maxConcurrent, 1);
  assert.deepEqual(calls, [
    { ref: "entity:harbor", x: 1, y: 2, z: 3 },
    { ref: "entity:harbor", x: 7, y: 8, z: 9 },
  ]);
  assert.deepEqual(queue.snapshot(), []);
});

test("a journal snapshot can be restored and flushed after the page is recreated", async () => {
  const original = createGraphPositionQueue({
    async save() { throw new Error("closing while offline"); },
  });
  await original.enqueue("entity:archive", { x: -4, y: 5, z: 6 });
  const journal = original.snapshot();

  const replayed = [];
  const restored = createGraphPositionQueue({
    async save(ref, position) { replayed.push({ ref, ...position }); },
  });
  restored.restore(journal);
  assert.equal(restored.pendingCount(), 1);
  assert.equal(await restored.flush(), true);
  assert.deepEqual(replayed, [{ ref: "entity:archive", x: -4, y: 5, z: 6 }]);
  assert.equal(restored.pendingCount(), 0);
});
