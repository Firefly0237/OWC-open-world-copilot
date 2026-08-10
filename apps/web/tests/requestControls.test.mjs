import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "typescript";

const [askSource, sweepSource, serviceSchemas] = await Promise.all([
  readFile(new URL("../src/pages/AskPage.vue", import.meta.url), "utf8"),
  readFile(new URL("../src/pages/SweepPage.vue", import.meta.url), "utf8"),
  readFile(new URL("../../../src/owcopilot/service/schemas.py", import.meta.url), "utf8"),
]);

function scriptSetup(source) {
  const start = source.indexOf("<script setup");
  assert.notEqual(start, -1, "script setup must exist");
  const bodyStart = source.indexOf(">", start) + 1;
  const end = source.indexOf("</script>", bodyStart);
  assert.notEqual(end, -1, "script setup must close");
  return source.slice(bodyStart, end);
}

function withoutImports(source) {
  const file = ts.createSourceFile("page.ts", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
  const imports = file.statements.filter((statement) => ts.isImportDeclaration(statement));
  let result = source;
  for (const statement of imports.toReversed()) {
    result = result.slice(0, statement.getFullStart()) + result.slice(statement.end);
  }
  return result;
}

function instantiate(source, dependencies, exposed) {
  const output = ts.transpileModule(withoutImports(scriptSetup(source)), {
    compilerOptions: {
      target: ts.ScriptTarget.ES2022,
      module: ts.ModuleKind.None,
    },
  }).outputText;
  const names = Object.keys(dependencies);
  const factory = new Function(...names, `${output}\nreturn { ${exposed.join(", ")} };`);
  return factory(...names.map((name) => dependencies[name]));
}

function ref(value) {
  return { value };
}

function computed(getter) {
  return {
    get value() {
      return getter();
    },
  };
}

function toRef(object, key) {
  return {
    get value() {
      return object[key];
    },
    set value(value) {
      object[key] = value;
    },
  };
}

function toRefs(object) {
  return Object.fromEntries(Object.keys(object).map((key) => [key, toRef(object, key)]));
}

test("ask and context preview dynamically send the same validated token budget", async () => {
  const calls = [];
  const page = instantiate(
    askSource,
    {
      ref,
      computed,
      onMounted() {},
      onUnmounted() {},
      addSessionCost() {},
      apiGet: async () => ({}),
      apiPost: async (path, payload) => {
        calls.push({ path, payload });
        if (path.endsWith("/ask")) {
          return {
            answer: { answer: "ok", refused: false, citations: [], faithfulness: null },
          };
        }
        return { refs: [], hits: [] };
      },
      costOf: () => 0,
      currentProject: () => "demo",
      llmConfig: () => ({ ready: true }),
      // Deliberately include a conflicting value: the explicit UI control must win.
      llmParams: () => ({ llm_mode: "offline", budget_tokens: 9999 }),
      streamJobEvents: async () => {},
      notifyError(error) {
        throw error;
      },
      example: () => "example",
    },
    ["ask", "previewContextPack", "question", "contextBudget", "contextBudgetValid"],
  );

  page.question.value = "雾隐盟是什么";
  page.contextBudget.value = 1379;
  await page.previewContextPack();
  await page.ask();

  assert.equal(page.contextBudgetValid.value, true);
  assert.deepEqual(
    calls.map(({ path, payload }) => [path, payload.budget_tokens]),
    [
      ["/projects/demo/context:pack", 1379],
      ["/projects/demo/ask", 1379],
    ],
  );

  for (const invalid of [0, 8001, 1.5, Number.NaN]) {
    page.question.value = "不能提交";
    page.contextBudget.value = invalid;
    assert.equal(page.contextBudgetValid.value, false);
    await page.previewContextPack();
    await page.ask();
  }
  assert.equal(calls.length, 2, "invalid budgets must not reach either endpoint");
});

test("theme sweep dynamically forwards bounded judge and semantic controls", async () => {
  const starts = [];
  const channel = {
    running: false,
    stageIndex: -1,
    elapsed: 0,
    result: null,
    hint: "",
    cost: 0,
  };
  const page = instantiate(
    sweepSource,
    {
      ref,
      computed,
      toRef,
      toRefs,
      onMounted() {},
      apiPost: async () => ({}),
      currentOperator: () => "reviewer",
      currentProject: () => "demo",
      llmConfig: () => ({ ready: true }),
      // Deliberately include conflicting values: the bounded page controls must win.
      llmParams: () => ({
        llm_mode: "real",
        llm_model: "judge",
        max_judge: 9999,
        semantic_threshold: 2,
      }),
      setCurrentOperator() {},
      getJobChannel: () => channel,
      resumeJob: async () => {},
      startJob: async (_key, options) => {
        starts.push(options);
      },
      notifyError(error) {
        throw error;
      },
      notifyOk() {},
      pushToast() {},
      example: () => "example",
    },
    [
      "run",
      "theme",
      "maxJudge",
      "semanticThreshold",
      "maxJudgeValid",
      "semanticThresholdValid",
    ],
  );

  page.theme.value = "守灯";
  page.maxJudge.value = 73;
  page.semanticThreshold.value = 0.42;
  await page.run();

  assert.equal(page.maxJudgeValid.value, true);
  assert.equal(page.semanticThresholdValid.value, true);
  assert.equal(starts.length, 1);
  assert.equal(starts[0].kind, "theme_sweep");
  assert.equal(starts[0].params.max_judge, 73);
  assert.equal(starts[0].params.semantic_threshold, 0.42);

  for (const [judge, threshold] of [
    [0, 0.5],
    [2001, 0.5],
    [1.5, 0.5],
    [400, -0.01],
    [400, 1.01],
    [400, Number.NaN],
  ]) {
    page.maxJudge.value = judge;
    page.semanticThreshold.value = threshold;
    await page.run();
  }
  assert.equal(starts.length, 1, "invalid sweep controls must not create jobs");

  for (const [judge, threshold] of [
    [1, 0],
    [2000, 1],
  ]) {
    page.maxJudge.value = judge;
    page.semanticThreshold.value = threshold;
    await page.run();
  }
  assert.deepEqual(
    starts.slice(1).map(({ params }) => [params.max_judge, params.semantic_threshold]),
    [
      [1, 0],
      [2000, 1],
    ],
  );
});

test("frontend controls remain aligned with the FastAPI request contract", () => {
  assert.match(
    serviceSchemas,
    /class ProjectAskRequest[\s\S]*?budget_tokens: int = Field\(default=800, ge=1, le=8000\)/,
  );
  assert.match(
    serviceSchemas,
    /"theme_sweep": \{[\s\S]*?"max_judge"[\s\S]*?"semantic_threshold"[\s\S]*?\}/,
  );

  assert.match(askSource, /:disabled="busy \|\| !question\.trim\(\) \|\| !llmReady \|\| !contextBudgetValid"/);
  assert.match(sweepSource, /v-model\.number="maxJudge"[\s\S]*?min="1"[\s\S]*?max="2000"/);
  assert.match(sweepSource, /v-model\.number="semanticThreshold"[\s\S]*?min="0"[\s\S]*?max="1"/);
  assert.match(sweepSource, /超出上限的对象不会丢弃，会保留为人工待查/);
  assert.match(sweepSource, /仅在真实语义模型可用时生效/);
});
