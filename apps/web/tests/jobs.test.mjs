import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "typescript";

const jobsUrl = new URL("../src/jobs.ts", import.meta.url).href;
const dataModule = (source) => `data:text/javascript,${encodeURIComponent(source)}`;

const vueMock = dataModule("export const reactive = (value) => value;");
const apiMock = dataModule(`
  const runtime = () => globalThis.__owJobTestRuntime;
  export class ApiError extends Error {
    constructor(status, detail) {
      super(String(status) + " " + detail);
      this.status = status;
      this.detail = detail;
    }
  }
  globalThis.__owJobTestApiError = ApiError;
  export const addSessionCost = (usd) => runtime().addSessionCost(usd);
  export const apiGet = (path) => runtime().apiGet(path);
  export const apiPost = (path, body) => runtime().apiPost(path, body);
  export const costOf = (result) => Number(result?.cost_budget?.used_usd ?? 0);
  export const currentProject = () => runtime().project;
  export const streamJobEvents = (jobId, onEvent) => runtime().streamJobEvents(jobId, onEvent);
`);
const toastMock = dataModule(`
  export const notifyError = (error) => globalThis.__owJobTestRuntime.notifications.push(error);
`);
const jobsSource = await readFile(new URL("../src/jobs.ts", import.meta.url), "utf8");
const jobsJavaScript = ts.transpileModule(jobsSource, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
}).outputText;

class MemoryStorage {
  constructor(seed = {}, reject = () => false) {
    this.values = new Map(Object.entries(seed));
    this.reject = reject;
  }

  getItem(key) {
    return this.values.get(key) ?? null;
  }

  setItem(key, value) {
    if (this.reject(key, value)) throw new DOMException("quota exceeded", "QuotaExceededError");
    this.values.set(key, value);
  }

  removeItem(key) {
    this.values.delete(key);
  }
}

const stages = [
  { key: "accepted", label: "受理" },
  { key: "done", label: "完成" },
];
const persistedKey = (key) => `owcopilot_job_v1:demo:${encodeURIComponent(key)}`;
let moduleSequence = 0;

function savedChannel(key, overrides = {}) {
  return {
    project: "demo",
    jobId: `job-${key}`,
    running: false,
    recoverable: true,
    started: true,
    stageIndex: 0,
    elapsed: 3,
    startedAt: Date.now() - 3_000,
    hint: "等待恢复",
    result: null,
    cost: 0,
    costAccountedJobId: "",
    ...overrides,
  };
}

function runtime(overrides = {}) {
  return {
    project: "demo",
    notifications: [],
    postCalls: [],
    getCalls: [],
    streamCalls: [],
    costCalls: [],
    async apiPost(path, body) {
      this.postCalls.push({ path, body });
      return { job_id: "job-new" };
    },
    async apiGet(path) {
      this.getCalls.push(path);
      return { status: "done", result: { marker: "restored" }, error: null };
    },
    async streamJobEvents(jobId) {
      this.streamCalls.push(jobId);
    },
    addSessionCost(usd) {
      this.costCalls.push(usd);
    },
    ...overrides,
  };
}

async function loadJobs(testRuntime, storage) {
  globalThis.__owJobTestRuntime = testRuntime;
  globalThis.sessionStorage = storage;
  globalThis.window = {
    setInterval: globalThis.setInterval,
    clearInterval: globalThis.clearInterval,
  };
  moduleSequence += 1;
  const isolatedSource = jobsJavaScript
    .replace('from "vue";', `from ${JSON.stringify(vueMock)};`)
    .replace('from "./api";', `from ${JSON.stringify(apiMock)};`)
    .replace('from "./toast";', `from ${JSON.stringify(toastMock)};`)
    .concat(`\n//# sourceURL=${jobsUrl}?jobs-test=${moduleSequence}`);
  return import(dataModule(isolatedSource));
}

test("startJob resumes a recoverable job instead of posting a duplicate", async () => {
  const key = "recover-before-start";
  const storage = new MemoryStorage({
    [persistedKey(key)]: JSON.stringify(savedChannel(key, { jobId: "job-existing" })),
  });
  const testRuntime = runtime();
  const jobs = await loadJobs(testRuntime, storage);

  await jobs.startJob(key, {
    kind: "world_seed",
    stages,
    params: { brief: "must not be posted" },
    parseResult: (raw) => raw,
  });

  assert.equal(testRuntime.postCalls.length, 0);
  assert.deepEqual(testRuntime.streamCalls, ["job-existing"]);
  assert.deepEqual(testRuntime.getCalls, ["/jobs/job-existing"]);
  assert.equal(jobs.getJobChannel(key, stages).result.marker, "restored");
});

test("quota fallback remains recoverable and restored cost is accounted exactly once", async () => {
  const key = "large-result";
  const largeResult = {
    payload: "x".repeat(20_000),
    cost_budget: { used_usd: 1.25 },
  };
  const storage = new MemoryStorage({}, (_key, value) => {
    const parsed = JSON.parse(value);
    return parsed.result !== null;
  });
  const costCalls = [];
  const firstRuntime = runtime({
    costCalls,
    async apiGet(path) {
      this.getCalls.push(path);
      return { status: "done", result: largeResult, error: null };
    },
  });
  const firstJobs = await loadJobs(firstRuntime, storage);

  await firstJobs.startJob(key, {
    kind: "world_seed",
    stages,
    params: {},
    parseResult: (raw) => raw,
  });

  const fallback = JSON.parse(storage.getItem(persistedKey(key)));
  assert.equal(fallback.jobId, "job-new");
  assert.equal(fallback.result, null);
  assert.equal(fallback.recoverable, true);
  assert.equal(fallback.costAccountedJobId, "job-new");
  assert.deepEqual(costCalls, [1.25]);

  const restoredRuntime = runtime({
    costCalls,
    async apiGet(path) {
      this.getCalls.push(path);
      return { status: "done", result: largeResult, error: null };
    },
  });
  const restoredJobs = await loadJobs(restoredRuntime, storage);
  assert.equal(restoredJobs.getJobChannel(key, stages).recoverable, true);

  await restoredJobs.resumeJob(key, {
    stages,
    parseResult: (raw) => raw,
  });

  assert.deepEqual(costCalls, [1.25]);
  assert.equal(restoredJobs.getJobChannel(key, stages).result.payload.length, 20_000);
});

for (const [source, status] of [
  ["SSE", 404],
  ["GET", 410],
]) {
  test(`${source} ${status} clears an expired channel instead of retrying forever`, async () => {
    const key = `missing-${source.toLowerCase()}`;
    const storage = new MemoryStorage({
      [persistedKey(key)]: JSON.stringify(savedChannel(key, { jobId: `job-${source}` })),
    });
    const testRuntime = runtime({
      async streamJobEvents(jobId) {
        this.streamCalls.push(jobId);
        if (source === "SSE") throw new globalThis.__owJobTestApiError(status, "job not found");
      },
      async apiGet(path) {
        this.getCalls.push(path);
        if (source === "GET") throw new globalThis.__owJobTestApiError(status, "job gone");
        return { status: "done", result: { marker: "unused" }, error: null };
      },
    });
    const jobs = await loadJobs(testRuntime, storage);

    await jobs.resumeJob(key, {
      stages,
      parseResult: (raw) => raw,
    });

    const channel = jobs.getJobChannel(key, stages);
    assert.equal(channel.jobId, "");
    assert.equal(channel.recoverable, false);
    assert.equal(channel.started, false);
    assert.equal(channel.stageIndex, -1);
    assert.match(channel.hint, /过期|重启/);
    assert.equal(testRuntime.notifications.length, 1);

    const saved = JSON.parse(storage.getItem(persistedKey(key)));
    assert.equal(saved.jobId, "");
    assert.equal(saved.recoverable, false);
  });
}
