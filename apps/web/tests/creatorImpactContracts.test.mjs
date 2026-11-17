import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";
import { computed, reactive, ref } from "vue";

const file = await readFile(new URL("../src/components/StoryPatchCard.vue", import.meta.url), "utf8");
const start = file.indexOf(">", file.indexOf("<script")) + 1;
const source = ts.createSourceFile("StoryPatchCard.vue", file.slice(start, file.indexOf("</script>", start)), ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
function declaration(name) {
  for (const statement of source.statements) {
    if (ts.isFunctionDeclaration(statement) && statement.name?.text === name) return statement.getText(source);
    if (ts.isVariableStatement(statement) && statement.declarationList.declarations.some(item => item.name.getText(source) === name)) return statement.getText(source);
  }
  throw new Error(`Missing production declaration ${name}`);
}
function setup(api) {
  const props = reactive({ project: "project", taskId: "task", revision: 1, source: { artifact_id: "draft", artifact_hash: "source" }, continuation: { task_preview_id: "play", task_preview_seq: 0 }, locked: false });
  const review = ref({ id: "review", review_hash: "report", report: { status: "ready_for_author" } });
  const env = {
    computed, props, review, api, alive: true, epoch: 0, humanizeError: String,
    snapshot: ref({ artifact_id: "draft", artifact_hash: "source" }), current: ref(true),
    receipt: ref(null), decision: ref(null), busy: ref(false), error: ref(""), notice: ref(""),
    applicationPreview: ref(null), impactBusy: ref(false), impactError: ref(""), impactKey: ref(""),
    sourceRevision: ref(1), readApplied: async () => {}, working: () => {},
  };
  const names = ["impactGeneration", "impactContextKey", "impactReady", "refreshImpact", "apply", "decide"];
  const code = names.map(declaration).join("\n") + `\nreturn { ${names.join(", ")} };`;
  const output = ts.transpileModule(code, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None } }).outputText;
  return { ...env, ...new Function(...Object.keys(env), output)(...Object.values(env)) };
}
const result = () => ({
  kind: "content_application_preview", preview_hash: "a".repeat(64), review_id: "review", review_hash: "report",
  task_id: "task", task_revision: 1, source_artifact_id: "draft", source_artifact_hash: "source",
  impact: { reading: { target_exists: true } }, writes_draft: false, writes_canon: false,
});

test("late impact responses cannot authorize a changed continuation position", async () => {
  let resolve;
  let applies = 0;
  const state = setup({ previewApplication: () => new Promise(done => { resolve = done; }), applyIntent: async () => { applies += 1; } });
  const pending = state.refreshImpact();
  state.props.continuation.task_preview_seq = 1;
  resolve(result());
  await pending;
  assert.equal(state.applicationPreview.value, null);
  assert.equal(state.impactReady.value, false);
  state.decision.value = { decision: "accepted", decision_hash: "decision" };
  await state.apply();
  assert.equal(applies, 0);
});

test("a current read-only preview binds the subsequent apply to its exact hash", async () => {
  const calls = [];
  const state = setup({ previewApplication: async () => result(), applyIntent: async (...args) => { calls.push(args); return { id: "receipt" }; } });
  await state.refreshImpact();
  assert.equal(state.impactReady.value, true);
  state.decision.value = { decision: "accepted", decision_hash: "decision" };
  await state.apply();
  assert.equal(calls.length, 1);
  assert.equal(calls[0][4], "a".repeat(64));
  assert.equal(calls[0][3].task_preview_seq, 0);
});

test("an unavailable impact blocks acceptance but still permits declining the suggestion", async () => {
  const choices = [];
  const state = setup({ decideIntent: async (...args) => { choices.push(args[3]); return { decision: args[3] }; } });
  await state.decide("accepted");
  assert.deepEqual(choices, []);
  await state.decide("rejected");
  assert.deepEqual(choices, ["rejected"]);
});

test("a response naming another draft is never shown as current impact", async () => {
  const state = setup({ previewApplication: async () => ({ ...result(), source_artifact_id: "other" }) });
  await state.refreshImpact();
  assert.equal(state.applicationPreview.value, null);
  assert.equal(state.impactReady.value, false);
  assert.match(state.impactError.value, /不一致/);
});
