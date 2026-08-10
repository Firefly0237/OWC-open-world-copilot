import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "typescript";

const source = await readFile(
  new URL("../src/pages/EngineeringPage.vue", import.meta.url),
  "utf8",
);

function scriptSetup(page) {
  const start = page.indexOf("<script setup");
  const bodyStart = page.indexOf(">", start) + 1;
  const end = page.indexOf("</script>", bodyStart);
  assert.ok(start >= 0 && bodyStart > start && end > bodyStart);
  return page.slice(bodyStart, end);
}

function withoutImports(input) {
  const file = ts.createSourceFile("EngineeringPage.ts", input, ts.ScriptTarget.Latest, true);
  let output = input;
  for (const statement of file.statements.filter(ts.isImportDeclaration).toReversed()) {
    output = output.slice(0, statement.getFullStart()) + output.slice(statement.end);
  }
  return output;
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

function translation(status = "needs_confirmation") {
  return {
    quest_id: "q_logic",
    translator_mode: "deterministic",
    used_llm: false,
    degraded: false,
    degradation_reasons: [],
    telemetry: { calls: 0, input_tokens: 0, output_tokens: 0, total_cost_usd: 0, cost_is_estimate: false },
    cost_budget: { budget_usd: null, used_usd: 0, remaining_usd: null, over_budget: false, cost_is_estimate: false },
    writes_canon: false,
    outcome: {
      intent: "『shared』应当可达",
      status,
      draft: { pattern: "REACHABLE", operands: ["shared"], description: "shared reachable" },
      groundings: [],
      spec: status === "needs_confirmation" ? { id: "q_logic_authored_123", pattern: "EF", expression: "shared" } : null,
      back_translation: status === "needs_confirmation" ? "『shared』可以到达。" : "",
      message: status === "needs_confirmation" ? "请确认" : "已拒绝",
    },
  };
}

function harness() {
  const calls = [];
  const costs = [];
  const toasts = [];
  const javascript = ts.transpileModule(withoutImports(scriptSetup(source)), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None },
  }).outputText;
  const dependencies = {
    ref,
    computed,
    onMounted() {},
    onUnmounted() {},
    ApiError: class ApiError extends Error { status = 500; },
    addSessionCost(value) { costs.push(value); },
    apiGet: async () => ({}),
    apiPost: async (path, payload) => {
      calls.push({ path, payload });
      if (path.endsWith("specs:translate")) return translation();
      return { quest_id: "q_logic", review_item_id: "review-1", writes_canon: false };
    },
    costOf: () => 0,
    currentProject: () => "demo",
    humanizeError: (error) => String(error),
    llmConfig: () => ({ ready: true }),
    llmParams: () => ({ llm_mode: "real", llm_model: "test-model" }),
    notifyError() {},
    pushToast(message, kind) { toasts.push({ message, kind }); },
  };
  const names = Object.keys(dependencies);
  const exposed = [
    "runSpecTranslate",
    "submitTemporalSpec",
    "questId",
    "specIntent",
    "specUseModel",
    "specMaxCostUsd",
    "specResult",
    "specTranslatedQuestId",
    "specSubmitResult",
  ];
  const factory = new Function(...names, `${javascript}\nreturn { ${exposed.join(", ")} };`);
  return { page: factory(...names.map((name) => dependencies[name])), calls, costs, toasts };
}

test("spec authoring separates deterministic translation from server-replayed review submission", async () => {
  const { page, calls, costs, toasts } = harness();
  page.questId.value = "q_logic";
  page.specIntent.value = "  『shared』应当可达  ";

  await page.runSpecTranslate();
  assert.equal(calls.length, 1);
  assert.match(calls[0].path, /\/quests\/q_logic\/specs:translate$/);
  assert.deepEqual(calls[0].payload, {
    intent: "『shared』应当可达",
    use_llm: false,
    llm_mode: "offline",
  });
  assert.equal(page.specTranslatedQuestId.value, "q_logic");
  assert.equal(page.specResult.value.writes_canon, false);
  assert.deepEqual(costs, [0]);

  // Changing the form after translation must not retarget the reviewed draft.
  page.questId.value = "q_other";
  page.specIntent.value = "different intent";
  await page.submitTemporalSpec();
  assert.equal(calls.length, 2);
  assert.match(calls[1].path, /\/quests\/q_logic\/specs:submit$/);
  assert.deepEqual(calls[1].payload, {
    intent: "『shared』应当可达",
    draft: { pattern: "REACHABLE", operands: ["shared"], description: "shared reachable" },
  });
  assert.equal(page.specSubmitResult.value.review_item_id, "review-1");
  assert.deepEqual(toasts.map(({ kind }) => kind), ["ok"]);
});

test("real translation is explicit and rejected outcomes cannot be submitted", async () => {
  const { page, calls } = harness();
  page.questId.value = "q_logic";
  page.specIntent.value = "find the reachable state";
  page.specUseModel.value = true;
  page.specMaxCostUsd.value = 0.25;

  await page.runSpecTranslate();
  assert.deepEqual(calls[0].payload, {
    intent: "find the reachable state",
    use_llm: true,
    llm_mode: "real",
    llm_model: "test-model",
    max_cost_usd: 0.25,
  });

  page.specResult.value = translation("rejected");
  await page.submitTemporalSpec();
  assert.equal(calls.length, 1, "a rejected or unassembled outcome must never reach submit");
});
