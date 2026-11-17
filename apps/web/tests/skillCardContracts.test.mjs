import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";
import { computed, reactive, ref } from "vue";

async function script(path) {
  const file = await readFile(new URL(path, import.meta.url), "utf8");
  const start = file.indexOf(">", file.indexOf("<script")) + 1;
  return ts.createSourceFile(path, file.slice(start, file.indexOf("</script>", start)), ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
}
const panel = await script("../src/components/StorySkillPanel.vue");
const page = await script("../src/pages/StoryCreatorPage.vue");
function declaration(source, name) {
  for (const statement of source.statements) {
    if (ts.isFunctionDeclaration(statement) && statement.name?.text === name) return statement.getText(source);
    if (ts.isVariableStatement(statement) && statement.declarationList.declarations.some(item => item.name.getText(source) === name)) return statement.getText(source);
  }
  throw new Error(`Missing production declaration ${name}`);
}
function execute(source, names, env) {
  const code = names.map(name => declaration(source, name)).join("\n") + `\nreturn { ${names.join(", ")} };`;
  const output = ts.transpileModule(code, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None } }).outputText;
  return new Function(...Object.keys(env), output)(...Object.values(env));
}
const cardRecord = (id = "card", status = "candidate") => ({
  id, card_hash: `hash-${id}`, name: "维护人物动机", purpose: "在局部对白修改时保留人物动机。",
  steps: [{ title: "定位", instruction: "读当前对白与人物目标。" }, { title: "修改", instruction: "在允许范围内修改。" }, { title: "检查", instruction: "保留人物目标。" }],
  applies_to: ["局部对白修订"], does_not_apply_to: ["重新设定人物目标"], source_summary: "一次已记录的运行",
  versions: [{ id: `${id}-v1`, version: 1, version_hash: `version-${id}`, status, changes: ["首次整理"], created_at: "2026-11-17" }],
  active_version_id: null, current_version_id: `${id}-v1`, enabled: true,
});
function setup(client = {}) {
  const emissions = [];
  const stored = new Map();
  const props = reactive({ project: "world", runId: "run", disabled: false, modelValue: [], applyProjectSkills: true });
  const env = {
    computed, ref, props, crypto, humanizeError: String, skillCardsClient: () => client,
    cards: ref([cardRecord()]), cardId: ref("card"), versionId: ref("card-v1"),
    loading: ref(false), working: ref(false), error: ref(""), notice: ref(""),
    name: ref(""), feedback: ref(""), job: ref(null), permissionUncertain: ref(false), submissionUncertain: ref(false),
    generation: 0, readSequence: 0, alive: true, pollJob: async () => {},
    sessionStorage: { setItem: (key, value) => stored.set(key, value), getItem: key => stored.get(key) ?? null, removeItem: key => stored.delete(key) },
    emit: (name, value) => { emissions.push({ name, value }); if (name === "update:modelValue") props.modelValue = value; },
  };
  const names = ["pendingOperation", "card", "version", "definition", "selected", "usable", "pendingJob", "unavailable", "setWorking", "rememberJob", "recoverJob", "chooseCard", "operationId", "reconcileSelections", "readCards", "useOnce", "mutate", "adopt", "setEnabled", "rollback", "summarize", "readManifest"];
  return { ...env, emissions, stored, ...execute(panel, names, env) };
}

test("a candidate can be selected once with an exact version hash without future adoption", () => {
  const state = setup({ adopt: () => { throw new Error("unexpected adoption"); } });
  state.useOnce();
  assert.deepEqual(state.props.modelValue, [{ card_id: "card", version_id: "card-v1", version_hash: "version-card" }]);
  assert.equal(state.cards.value[0].active_version_id, null);
  assert.match(state.notice.value, /下次生成/);
  state.useOnce();
  assert.deepEqual(state.props.modelValue, []);
});

test("only future adoption writes a default and does not select the current story content", async () => {
  const calls = [];
  const state = setup({ adopt: async (...args) => { calls.push(args); return { ...cardRecord("card", "accepted"), active_version_id: "card-v1" }; } });
  await state.adopt();
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0].card_hash, "hash-card");
  assert.equal(calls[0][1], "card-v1");
  assert.equal(calls[0][2], true);
  assert.equal(state.cards.value[0].active_version_id, "card-v1");
  assert.deepEqual(state.props.modelValue, []);
});

test("retired, disabled, or changed versions are removed from explicit next-run selections", () => {
  const state = setup();
  state.cards.value = [cardRecord("valid", "accepted"), { ...cardRecord("disabled"), enabled: false }, cardRecord("retired", "retired"), cardRecord("changed")];
  state.props.modelValue = ["valid", "disabled", "retired", "changed"].map(id => ({ card_id: id, version_id: `${id}-v1`, version_hash: id === "changed" ? "old-hash" : `version-${id}` }));
  state.reconcileSelections();
  assert.deepEqual(state.props.modelValue.map(item => item.card_id), ["valid"]);
});

test("a late list from another workspace cannot replace cards or authorize a selection", async () => {
  let resolve;
  const state = setup({ list: () => new Promise(done => { resolve = done; }) });
  const pending = state.readCards();
  state.props.project = "other";
  resolve({ cards: [cardRecord("foreign")] });
  await pending;
  assert.equal(state.cards.value[0].id, "card");
  assert.equal(state.emissions.some(item => item.name === "update:modelValue"), false);
});

test("an unknown mutation result clears local permission until an authoritative refresh", async () => {
  const state = setup({ enable: async () => { throw new Error("response lost"); }, list: async () => ({ cards: [{ ...cardRecord(), enabled: false }] }) });
  state.useOnce();
  await state.setEnabled();
  assert.equal(state.permissionUncertain.value, true);
  assert.deepEqual(state.props.modelValue, []);
  state.useOnce();
  assert.deepEqual(state.props.modelValue, []);
  await state.readCards();
  assert.equal(state.permissionUncertain.value, false);
  assert.equal(state.usable.value, false);
});

test("lost summarization responses reuse the operation id and reject a changed retry payload", async () => {
  const calls = [];
  const state = setup({ fromRun: async (...args) => { calls.push(args); if (calls.length === 1) throw new Error("response lost"); return { job_id: "job", status: "queued" }; } });
  await state.summarize(false);
  assert.equal(state.submissionUncertain.value, true);
  state.name.value = "changed";
  await state.summarize(false);
  assert.equal(calls.length, 1);
  state.name.value = "";
  await state.summarize(false);
  assert.equal(calls.length, 2);
  assert.equal(calls[0][1], calls[1][1]);
  assert.equal(state.submissionUncertain.value, false);
});

test("updating a viewed historical version binds to that exact base rather than the latest candidate", async () => {
  const calls = [];
  const state = setup({ update: async (...args) => { calls.push(args); return { job_id: "job", status: "queued" }; } });
  state.cards.value[0].versions.push({ id: "card-v2", version: 2, version_hash: "v2", status: "candidate", changes: [], created_at: "2026-11-17" });
  state.cards.value[0].current_version_id = "card-v2";
  state.feedback.value = "保留当前步骤，增加失败后的检查。";
  await state.summarize(true);
  assert.equal(calls[0][1], "card-v1");
  assert.equal(calls[0][2], "run");
  assert.match(calls[0][3], /失败后的检查/);
});

test("recovering a remembered job only observes it and never resubmits a model task", () => {
  const state = setup({ fromRun: () => { throw new Error("unexpected submission"); } });
  state.rememberJob({ job_id: "recorded", status: "running" });
  state.recoverJob();
  assert.equal(state.job.value.job_id, "recorded");
  assert.equal(state.working.value, false);
  state.rememberJob({ job_id: "recorded", status: "succeeded" });
  assert.equal(state.stored.size, 0);
});

test("a late job observation cannot restore a running state after a newer terminal response", async () => {
  const replies = [];
  const env = {
    computed, props: reactive({ project: "world" }), generation: 0, alive: true,
    job: ref({ job_id: "job", status: "queued" }), cards: ref([]), notice: ref(""), error: ref(""),
    cardId: ref(""), pendingOperation: null, timer: undefined, permissionUncertain: ref(false),
    stopTimer: () => {}, rememberJob: () => {}, readCards: async () => {}, chooseCard: () => {}, humanizeError: String,
    skillCardsClient: () => ({ job: () => new Promise(done => replies.push(done)) }),
    setTimeout: () => { throw new Error("stale observation must not reschedule"); },
  };
  const state = execute(panel, ["jobReadSequence", "jobOutcome", "pollJob"], env);
  const first = state.pollJob("job", 0, "world");
  const newer = state.pollJob("job", 0, "world");
  replies[1]({ job_id: "job", status: "succeeded" });
  await newer;
  replies[0]({ job_id: "job", status: "running" });
  await first;
  assert.equal(env.job.value.status, "succeeded");
});

async function observeTerminal(response, visibleCards = [cardRecord("recorded")]) {
  let reads = 0;
  const env = {
    computed, props: reactive({ project: "world" }), generation: 0, alive: true,
    job: ref({ job_id: "job", status: "running" }), cards: ref([cardRecord("unrelated")]),
    notice: ref(""), error: ref("previous transient error"), feedback: ref("request"),
    cardId: ref("unrelated"), versionId: ref("unrelated-v1"), permissionUncertain: ref(false),
    pendingOperation: null, timer: undefined, stopTimer: () => {}, rememberJob: () => {},
    readCards: async () => { reads += 1; env.cards.value = visibleCards; }, humanizeError: String,
    skillCardsClient: () => ({ job: async () => response, fromRun: () => { throw new Error("must not redispatch"); } }),
    setTimeout: () => { throw new Error("terminal job must not reschedule"); },
  };
  const state = execute(panel, ["jobReadSequence", "jobOutcome", "recordingIssue", "pollJob"], env);
  await state.pollJob("job", 0, "world");
  return { ...env, reads, outcome: state.jobOutcome(env.job.value), recordingIssue: state.recordingIssue.value };
}

test("the formal done job DTO displays only its recorded candidate and leaves adoption unchanged", async () => {
  const state = await observeTerminal({
    job_id: "job", kind: "skill_summary", status: "done", events: [{ type: "done", data: {} }], error: null,
    result: { status: "candidate", card_id: "recorded", version_id: "recorded-v1", reason: "保留可复用步骤" },
  });
  assert.equal(state.reads, 1);
  assert.equal(state.cardId.value, "recorded");
  assert.equal(state.versionId.value, "recorded-v1");
  assert.match(state.notice.value, /候选已生成/);
  assert.equal(state.error.value, "");
  assert.equal(state.cards.value[0].active_version_id, null);
});

test("done with no_change reports no update without announcing a candidate", async () => {
  const state = await observeTerminal({ job_id: "job", status: "done", result: { status: "no_change", reason: "没有实质变化", new_dispatch: false } });
  assert.match(state.notice.value, /无需新增或更新/);
  assert.match(state.notice.value, /没有实质变化/);
  assert.doesNotMatch(state.notice.value, /候选已生成/);
  assert.equal(state.cardId.value, "unrelated");
  assert.equal(state.error.value, "");
});

test("done does not turn unknown, failed, or absent business receipts into success", async () => {
  for (const result of [{ status: "claimed_unknown", new_dispatch: false }, { status: "completed_failed", result: { error: "旧调用失败" } }, null]) {
    const state = await observeTerminal({ job_id: "job", status: "done", result });
    assert.equal(state.notice.value, "");
    assert.ok(state.error.value);
    assert.notEqual(state.outcome.kind, "candidate");
    assert.equal(state.cardId.value, "unrelated");
  }
});

test("completed replay receipts reveal the prior candidate without claiming a fresh dispatch", async () => {
  const state = await observeTerminal({ job_id: "job", status: "done", result: {
    status: "completed", new_dispatch: false,
    result: { status: "candidate", card_id: "recorded", version_id: "recorded-v1" },
  } });
  assert.equal(state.cardId.value, "recorded");
  assert.match(state.notice.value, /此前整理/);
  assert.match(state.notice.value, /没有重复调用/);
});

test("a candidate receipt naming a missing card is withheld until its actual version is read", async () => {
  const state = await observeTerminal({ job_id: "job", status: "done", result: { status: "candidate", card_id: "missing", version_id: "missing-v1" } });
  assert.equal(state.notice.value, "");
  assert.match(state.error.value, /候选记录尚未读回/);
});

test("a maintenance recording error preserves the generated candidate and reports the missing linkage", async () => {
  const state = await observeTerminal({ job_id: "job", status: "done", result: {
    status: "candidate", card_id: "recorded", version_id: "recorded-v1", maintenance_recording_error: "关联记录暂未保存",
  } });
  assert.equal(state.outcome.kind, "candidate");
  assert.equal(state.cardId.value, "recorded");
  assert.match(state.notice.value, /候选已生成/);
  assert.equal(state.error.value, "");
  assert.equal(state.recordingIssue, "关联记录暂未保存");
  assert.equal(state.cards.value[0].active_version_id, null);
});

test("file imports require a text manifest and pass no executable object fields", () => {
  const state = setup();
  assert.throws(() => state.readManifest('{}'), /文件清单/);
  assert.throws(() => state.readManifest('{"files":[{"path":"SKILL.md"}]}'), /文字内容/);
  assert.deepEqual(state.readManifest('{"files":[{"path":"skill/SKILL.md","content":"text","execute":true}],"adopt":true}'), { files: [{ path: "skill/SKILL.md", content: "text" }] });
});

test("friendly skill instructions label typed parameters without rebinding or leaking source instances", () => {
  const { friendlyInstruction } = execute(panel, ["friendlyInstruction"], {});
  const step = {
    title: "设置选择", instruction: "按{{options}}安排分支，保留{{stages}}，由{{actor}}回应；其余按{{future}}和{{missing}}处理。",
    slots: [
      { name: "options", kind: "requirement", binding_key: "decision_option_count", source_value: "3" },
      { name: "stages", kind: "requirement", binding_key: "stage_count", source_value: "4" },
      { name: "actor", kind: "entity", binding_key: "participant:1", source_value: "original-person" },
      { name: "future", kind: "requirement", binding_key: "future_key", source_value: "private-source" },
    ],
  };
  const before = structuredClone(step);
  assert.equal(friendlyInstruction(step), "按【本次选项数量】安排分支，保留【本次阶段数量】，由【本次第 2 位角色】回应；其余按【本次约定参数】和【本次约定参数】处理。");
  assert.deepEqual(step, before);
  assert.doesNotMatch(friendlyInstruction(step), /original-person|private-source|\{\{/);
});

function production(produce) {
  const env = {
    task: ref({ id: "task", revision: 1 }), narrativeProjectId: ref(""), narrativeReady: ref(true),
    narrativeSceneId: ref(""), narrativePreviewId: ref(""), narrativeTransitionId: ref(""),
    selection: ref("planned"), variant: ref("constrained"), machine: () => ({}), policy: ref({ mode: "delegate" }),
    applyProjectSkills: ref(false), autoSummarizeSkill: ref(true), skillSelections: ref([{ card_id: "card", version_id: "card-v1", version_hash: "version-card" }]),
    chosenVersion: ref(null), selectedTrial: ref(null), run: ref(null), generation: 0, alive: true,
    api: { produce }, baselineRunId: ref(""), expectingRun: ref(false), jobId: ref(""), job: ref(null), notice: ref(""),
    sessionStorage: { setItem: () => {} }, jobCacheKey: () => "job-key", chooseRun: () => {}, poll: async () => {},
  };
  return { ...env, ...execute(page, ["startProduction"], env) };
}
test("production sends explicit skill identities separately from defaults and consumes them after success", async () => {
  const calls = [];
  const state = production(async (...args) => { calls.push(args); return { job_id: "job" }; });
  await state.startProduction();
  assert.equal(calls[0][1].selection, "planned");
  assert.equal(calls[0][1].apply_project_skills, false);
  assert.equal(calls[0][1].summarize_skill, true);
  assert.deepEqual(calls[0][1].skill_card_selections, [{ card_id: "card", version_id: "card-v1", version_hash: "version-card" }]);
  assert.deepEqual(state.skillSelections.value, []);
});

test("a rejected production request preserves the explicit next-run choice for a deliberate retry", async () => {
  const state = production(async () => { throw new Error("not admitted"); });
  await assert.rejects(state.startProduction(), /not admitted/);
  assert.equal(state.skillSelections.value.length, 1);
});
