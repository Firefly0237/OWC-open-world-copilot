import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";
import { computed, effectScope, nextTick, reactive, ref, watch } from "vue";

// These fixtures execute production functions without mounting a page or calling a server.
async function script(path) {
  const file = await readFile(new URL(path, import.meta.url), "utf8");
  const start = file.indexOf(">", file.indexOf("<script")) + 1;
  const text = file.slice(start, file.indexOf("</script>", start));
  return ts.createSourceFile(path, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
}
const page = await script("../src/pages/StoryCreatorPage.vue");
const trial = await script("../src/components/StoryMethodTrial.vue");

function declaration(source, name) {
  for (const statement of source.statements) {
    if (ts.isFunctionDeclaration(statement) && statement.name?.text === name) return statement.getText(source);
    if (ts.isVariableStatement(statement) && statement.declarationList.declarations.some(item => item.name.getText(source) === name)) return statement.getText(source);
  }
  throw new Error(`Missing production declaration: ${name}`);
}
function execute(source, names, env, extra = "") {
  const code = `${names.map(name => declaration(source, name)).join("\n")}\n${extra}\nreturn { ${names.join(", ")} };`;
  const output = ts.transpileModule(code, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None } }).outputText;
  return new Function(...Object.keys(env), output)(...Object.values(env));
}
function memoryStorage() {
  const entries = new Map();
  return { getItem: key => entries.get(key) ?? null, setItem: (key, value) => entries.set(key, String(value)), removeItem: key => entries.delete(key) };
}
function refNames(source) {
  return source.statements.filter(ts.isVariableStatement).flatMap(statement => statement.declarationList.declarations)
    .filter(item => item.initializer && ts.isCallExpression(item.initializer) && item.initializer.expression.getText(source) === "ref")
    .map(item => item.name.getText(source));
}
function creator(api = {}) {
  const storage = memoryStorage();
  const functions = ["object", "isQuest", "isTree", "raw", "numberInput", "machine", "restoreInputs", "editorKey", "jobCacheKey", "saveEditorDraft", "restoreEditorDraft", "stopPolling", "clearRun", "newDraft", "openTask", "perform", "patchApplied", "restoreReading", "normalizeTaskPreview", "continueEdited", "continueOriginalTask", "readPendingTaskPreview", "useTrial"];
  const value = execute(page, [...refNames(page), "generation", "alive", "timer", "gate", "taskPreviewIsHistorical", ...functions], {
    ref, computed, api, currentProject: () => "project", sessionStorage: storage,
    taskOrigin: value => value.origin_kind ?? "unspecified", taskTitle: value => value.request.brief,
    poll: async () => {}, recoverApplication: async () => {}, humanizeError: String,
    quest: ref(null), tree: ref(null), clearTimeout,
  });
  return { ...value, storage };
}
const taskRecord = (id, revision = 1) => ({ id, revision, origin_kind: "author", request: { brief: id, protected: [] } });
const policy = { mode: "direction", plan_review: true, key_step_review: false, allow_read_tools: true, allow_candidate_trial: false };
const runRecord = (taskId, gateHash, revision = 1) => ({
  run: { id: `run-${taskId}-${gateHash}`, revision, status: "waiting_user" },
  collaboration: { policy: { ...policy }, pending_gate: { gate: { gate_hash: gateHash, node_id: "draft" } } },
});
const quest = id => ({ id: `quest-${id}`, title: id, objective: id, stages: [] });
const tree = id => ({ id: `tree-${id}`, nodes: {} });
const artifact = id => ({ id, content_hash: `hash-${id}`, payload: { quest: quest(id), tree: tree(id) } });

test("restoring formal requirements preserves hidden rules and unchanged role sources", () => {
  const state = creator();
  const requirements = {
    schema_version: "1.0", stage_count: 3, decision_count: 1, decision_option_count: 2, distinct_outcomes: true,
    participant_ids: ["person"], game_state_variables: [{ name: "trust", value_type: "integer" }],
    business_roles: [{ id: "role", role: "creditor", entity_id: "person", source_refs: ["source:contract"] }],
    business_relations: [{ id: "debt", from_role_id: "role", to_role_id: "other", relation: "owes" }],
  };
  state.restoreInputs({ requirements: { machine: requirements }, narrative_preview_id: "route", narrative_transition_id: "turn" });
  state.stageCount.value = "4";
  const actual = state.machine();
  assert.deepEqual(actual, { ...requirements, stage_count: 4 });
  assert.notEqual(state.savedRequirements.value, requirements);
  assert.equal(requirements.stage_count, 3);
  state.businessRoles.value[0].role = "supplier";
  assert.deepEqual(state.machine().business_roles[0].source_refs, ["author_structured"]);
  assert.equal(state.narrativePreviewId.value, "route");
  assert.equal(state.narrativeTransitionId.value, "turn");
  state.newDraft();
  assert.equal(state.narrativePreviewId.value, "");
  assert.equal(state.narrativeTransitionId.value, "");
  assert.deepEqual(state.savedRequirements.value, {});
});

test("task switches retain drafts only for their task revision and exact stopping point", async () => {
  const records = { A: { task: taskRecord("A"), runs: [runRecord("A", "gate-A")] }, B: { task: taskRecord("B"), runs: [runRecord("B", "gate-B")] } };
  const state = creator({ detail: async id => records[id], taskArtifacts: async () => ({ artifacts: [] }) });
  await state.openTask("A");
  state.feedback.value = "A feedback"; state.feedbackKind.value = "workflow"; state.feedbackTarget.value = "draft_logic";
  state.gateNotes.value = "A gate edit"; state.gateTarget.value = "draft"; state.guidance.value = { draft: "A guidance" };
  state.gatePolicy.value = { ...policy, allow_read_tools: false };
  state.narrativePreviewId.value = "route-A"; state.narrativeTransitionId.value = "turn-A";
  state.useTrial({ version_id: "version-A", scope_id: "permit-A", permit_hash: "hash-A" });
  await state.openTask("B");
  const savedA = state.storage.getItem(state.editorKey("A"));
  assert.equal(state.feedback.value, ""); assert.equal(state.gateNotes.value, "");
  assert.equal(state.narrativePreviewId.value, ""); assert.equal(state.narrativeTransitionId.value, "");
  assert.equal(state.selectedTrial.value, null); assert.equal(state.chosenVersionId.value, "");
  state.feedback.value = "B feedback";
  await state.openTask("A");
  assert.equal(state.feedback.value, "A feedback"); assert.equal(state.feedbackKind.value, "workflow");
  assert.equal(state.gateNotes.value, "A gate edit"); assert.deepEqual(state.guidance.value, { draft: "A guidance" });
  assert.equal(state.gatePolicy.value.allow_read_tools, false);
  await state.openTask("B");
  assert.equal(state.feedback.value, "B feedback");
  state.storage.setItem(state.editorKey("A"), savedA);
  records.A.runs = [runRecord("A", "new-gate")];
  await state.openTask("A");
  assert.equal(state.feedback.value, "A feedback");
  assert.equal(state.gateNotes.value, ""); assert.deepEqual(state.guidance.value, {});
  assert.equal(state.priorStageDraft.value.gateNotes, "A gate edit");
  await state.openTask("B");
  state.storage.setItem(state.editorKey("A"), savedA);
  records.A = { task: taskRecord("A", 2), runs: [runRecord("A", "gate-A", 2)] };
  await state.openTask("A");
  assert.equal(state.feedback.value, ""); assert.equal(state.gateNotes.value, "");
  assert.equal(state.priorStageDraft.value.revision, 1);
  assert.equal(state.priorStageDraft.value.feedback, "A feedback");
});

test("a second edit cannot attach an unrelated preview or display another source as its original", async () => {
  const calls = [];
  const state = creator({
    continueEditedPreview: async (...args) => { calls.push(["edited", ...args]); throw new Error("unrelated preview must not be sent"); },
    artifact: async id => { calls.push(["artifact", id]); return { artifact: artifact(id) }; },
    continueOriginalTask: async (...args) => { calls.push(["original", ...args]); return { preview_id: "historical-A", view: { action_seq: 0, choices: [] }, version_mode: "fixed_historical" }; },
    readTaskPreview: async id => ({ preview_id: id, view: { action_seq: 0, choices: [] }, version_mode: "fixed_historical" }),
  });
  const previewA = { preview_id: "preview-A", view: { action_seq: 4, choices: [] } };
  state.preview.value = previewA; state.previewArtifactId.value = "A";
  state.previewPayload.value = { quest: quest("A"), tree: tree("A") };
  state.patchApplied({ artifact: artifact("B"), task: taskRecord("story", 2), receipt: { id: "edit-AB", revision: 2, source_artifact_id: "A", continuation: { task_preview_id: "preview-A", task_preview_seq: 4 } } });
  assert.equal(state.savedPreviewBeforeEdit.value.preview_id, "preview-A");
  state.patchApplied({ artifact: artifact("C"), task: taskRecord("story", 3), receipt: { id: "edit-BC", revision: 3, source_artifact_id: "B", continuation: {} } });
  assert.equal(state.savedPreviewBeforeEdit.value, null);
  assert.equal(state.preview.value.preview_id, "preview-A");
  assert.equal(state.previewArtifactId.value, "A");
  await state.continueEdited();
  assert.deepEqual(calls, []);
  await state.continueOriginalTask();
  assert.deepEqual(calls, [["artifact", "A"], ["original", "preview-A", 4]]);
  assert.equal(state.error.value, "");
  assert.equal(state.preview.value.version_mode, "fixed_historical");
  assert.equal(state.previewArtifactId.value, "A");
  assert.deepEqual(state.previewPayload.value, { quest: quest("A"), tree: tree("A") });
  assert.equal(state.appliedArtifact.value.id, "C");
});

test("only an explicit idle selection uses a permit, changes invalidate it and active work still allows revocation", async () => {
  const state = creator();
  const props = reactive({ project: "project", task: taskRecord("A"), version: { id: "candidate", version_hash: "candidate-hash", state: { trial_status: "allowed", author_acceptance: "pending" } }, familyHash: "family", requirements: { stage_count: 2 }, disabled: false, executionActive: false });
  const scope = { status: "active", permit: { id: "permit-A", permit_hash: "permit-hash", task_id: "A", task_revision: 1, allowed_versions: { candidate: "candidate-hash" }, requirements: { stage_count: 2 } } };
  let currentScope = scope;
  let delayed;
  let deferNext = false;
  const requests = [];
  const mutations = [];
  const owner = effectScope();
  const watches = trial.statements.filter(statement => ts.isExpressionStatement(statement)
    && ts.isCallExpression(statement.expression) && statement.expression.expression.getText(trial) === "watch")
    .map(statement => statement.getText(trial)).join("\n");
  const component = owner.run(() => execute(trial, [...refNames(trial), "generation", "readSequence", "alive", "grantOperationId", "stable", "requirementKey", "requirementsKey", "contextKey", "actorKind", "matching", "usable", "unavailable", "readScopes", "grant", "revoke", "selectCurrent"], {
    ref, computed, watch, props, humanizeError: String,
    emit: (event, value) => { if (event === "selection") state.useTrial(value); },
    storyCreatorClient: () => ({
      trialScopes: async id => {
        requests.push(id);
        if (deferNext) { deferNext = false; return new Promise(resolve => { delayed = resolve; }); }
        return { scopes: [currentScope] };
      },
      grantTaskTrial: async (...args) => { mutations.push(["grant", ...args]); return currentScope; },
      revokeTaskTrial: async (...args) => { mutations.push(["revoke", ...args]); currentScope = { ...scope, status: "revoked" }; return currentScope; },
    }),
  }, watches));
  const settle = async () => { await nextTick(); await Promise.resolve(); await nextTick(); };
  try {
    await settle();
    assert.equal(state.selectedTrial.value, null);
    component.selectCurrent();
    assert.equal(state.selectedTrial.value.scope_id, "permit-A");
    for (const change of [
      () => { props.requirements.stage_count = 3; },
      () => { props.task.revision = 2; },
      () => { props.version.version_hash = "changed-hash"; },
      () => { props.version.id = "other-candidate"; },
    ]) {
      change(); await settle();
      assert.equal(state.selectedTrial.value, null); assert.equal(state.chosenVersionId.value, "");
      props.task = taskRecord("A"); props.requirements = { stage_count: 2 };
      props.version = { id: "candidate", version_hash: "candidate-hash", state: { trial_status: "allowed", author_acceptance: "pending" } };
      await settle(); assert.equal(state.selectedTrial.value, null);
      component.selectCurrent(); assert.equal(state.selectedTrial.value.scope_id, "permit-A");
    }
    state.useTrial(null);
    props.executionActive = true;
    await component.grant(); component.selectCurrent();
    assert.equal(state.selectedTrial.value, null);
    assert.deepEqual(mutations, []);
    props.executionActive = false;
    await component.grant(); await settle();
    assert.equal(mutations[0][0], "grant");
    assert.equal(state.selectedTrial.value, null);
    component.selectCurrent();
    assert.equal(state.selectedTrial.value.scope_id, "permit-A");
    props.executionActive = true;
    await component.revoke(); await settle();
    assert.equal(mutations[1][0], "revoke");
    assert.equal(mutations[1][1], "permit-A"); assert.equal(mutations[1][2], "permit-hash");
    assert.equal(state.selectedTrial.value, null); assert.equal(component.matching.value, null);
    props.executionActive = false; currentScope = scope;
    await component.readScopes(); await settle();
    assert.equal(state.selectedTrial.value, null);
    component.selectCurrent();
    assert.equal(state.selectedTrial.value.scope_id, "permit-A");
    deferNext = true;
    const pending = component.readScopes();
    props.task = { ...taskRecord("B"), origin_kind: "development" }; await settle();
    assert.equal(state.selectedTrial.value, null);
    assert.equal(component.actorKind.value, "development_operator");
    delayed({ scopes: [scope] }); await pending; await settle();
    assert.equal(state.selectedTrial.value, null); assert.equal(state.chosenVersionId.value, "");
    assert.ok(requests.includes("B"));
  } finally { owner.stop(); }
});
