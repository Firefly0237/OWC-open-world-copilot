import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";
import { computed, reactive, ref } from "vue";

const file = await readFile(new URL("../src/components/StoryRunSummary.vue", import.meta.url), "utf8");
const start = file.indexOf(">", file.indexOf("<script")) + 1;
const source = ts.createSourceFile("StoryRunSummary.vue", file.slice(start, file.indexOf("</script>", start)), ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
function declaration(name) {
  for (const statement of source.statements) {
    if (ts.isFunctionDeclaration(statement) && statement.name?.text === name) return statement.getText(source);
    if (ts.isVariableStatement(statement) && statement.declarationList.declarations.some(item => item.name.getText(source) === name)) return statement.getText(source);
  }
  throw new Error(`Missing production declaration ${name}`);
}
function execute(names, env = {}) {
  const code = names.map(declaration).join("\n") + `\nreturn { ${names.join(", ")} };`;
  const output = ts.transpileModule(code, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None } }).outputText;
  return new Function(...Object.keys(env), output)(...Object.values(env));
}
const SYSTEM_CREATOR_CHECKS = new Set(["coverage_audit", "validate_decisions"]);
const CREATOR_OPERATIONS = { draft_quest: "场景梗概", coverage_audit: "检查分支与条件", validate_decisions: "检查选择与后果" };
const { turnState, modeText, usageStatus, usageSummary } = execute(["operationName", "turnState", "modeText", "usageStatus", "usageSummary"], { SYSTEM_CREATOR_CHECKS, CREATOR_OPERATIONS });
const { memorySummary } = execute(["memorySummary"]);
const { memoryLearningSummary } = execute(["memoryLearningSummary"]);

test("memory display distinguishes actual content, cold starts, already-bound skills and omissions", () => {
  assert.equal(memorySummary(null), "");
  const run = { memory_route: { backend: "trajectory", read_snapshot: { payload: { units: [] }, omitted_units: 0 } } };
  assert.equal(memorySummary(run), "本轮没有带入历史内容");
  run.memory_route.read_snapshot.payload.units.push({});
  run.memory_route.read_snapshot.omitted_units = 2;
  assert.equal(memorySummary(run), "带入 1 项历史记录；2 项未带入");
  run.memory_route.backend = "skill";
  run.memory_route.read_snapshot.payload = { skills: [] };
  run.memory_route.read_snapshot.already_bound_skill_versions = ["version-1"];
  assert.match(memorySummary(run), /已由本次选用提供/);
  run.memory_route.backend = "trajectory";
  run.memory_route.read_snapshot.already_bound_skill_versions = [];
  run.memory_route.read_snapshot.omitted_units = 0;
  run.memory_route.read_snapshot.payload = { messages: [{}, {}] };
  assert.equal(memorySummary(run), "带入 2 项历史消息");
  run.memory_route.backend = "strategy";
  run.memory_route.read_snapshot.payload = { lessons: [{}] };
  assert.equal(memorySummary(run), "带入 1 项参考做法");
});

test("memory formation does not claim adoption or hide maintenance failure", () => {
  const run = { memory_route: { backend: "strategy" }, memory_update: { status: "recorded", learning_status: "completed", lesson_count: 2 } };
  assert.equal(memoryLearningSummary(run), "已整理 2 条参考做法");
  run.memory_update.learning_status = "unknown";
  assert.equal(memoryLearningSummary(run), "经验整理结果待核对");
  run.memory_update.learning_status = "failed";
  assert.match(memoryLearningSummary(run), /未完成.*草稿保留/);
  run.memory_update.status = "not_recorded";
  assert.equal(memoryLearningSummary(run), "");
});
const runRecord = (status = "running") => ({
  run: { id: "run", revision: 2, status },
  collaboration: { policy: { mode: "edit", plan_review: true, key_step_review: true }, pending_gate: null },
  graph: { nodes: [{ id: "draft", operation: "draft_quest" }, { id: "audit", operation: "coverage_audit" }] },
});

test("a real pending gate gives the author the turn while terminal states override leftover gates", () => {
  const run = runRecord("waiting_user");
  run.collaboration.pending_gate = { gate: { title: "场景梗概", permission_required: false } };
  assert.equal(turnState(run, [], false).actor, "轮到你确认");
  assert.equal(turnState(run, [], false).target, "#creator-author-turn");
  run.run.status = "cancelled";
  assert.equal(turnState(run, [], false).actor, "本轮已停止");
  assert.equal(turnState(run, [], false).target, "");
});

test("an absent gate is not invented from waiting_user, and missing permission is a separate action", () => {
  const run = runRecord("waiting_user");
  assert.equal(turnState(run, [], false).actor, "停点信息待读回");
  run.collaboration.pending_gate = { title: "场景梗概", permission_required: true };
  assert.equal(turnState(run, [], false).actor, "等你调整约定");
});

test("live node events distinguish AI creative work from system checks without reading a graph UI", () => {
  const run = runRecord();
  const events = [{ seq: 2, type: "graph.node.started", node_id: "audit" }, { seq: 1, type: "graph.node.started", node_id: "draft" }];
  assert.equal(turnState(run, events, false).actor, "AI 正在制作");
  events.push({ seq: 3, type: "graph.node.completed", node_id: "draft" });
  assert.equal(turnState(run, events, false).actor, "系统正在检查与整理");
  run.collaboration.pause_requested = true;
  assert.match(turnState(run, events, false).detail, /当前步骤结束后暂停/);
});

test("the actual plan and step review booleans override preset names", () => {
  const run = runRecord();
  run.collaboration.policy = { mode: "edit", plan_review: false, key_step_review: false };
  assert.equal(modeText(run), "连续制作");
  run.collaboration.policy.review_operations = ["draft_quest"];
  assert.equal(modeText(run), "指定步骤确认");
  run.collaboration.policy.plan_review = true;
  assert.equal(modeText(run), "先确认大纲");
});

test("optional skill scheduling errors stay separate from a completed story", () => {
  const props = reactive({ run: runRecord("completed") });
  const state = execute(["summaryIssue"], { props, computed });
  assert.equal(state.summaryIssue.value, "");
  props.run.skill_summary_error = "整理服务暂不可用";
  assert.equal(state.summaryIssue.value, "整理服务暂不可用");
  assert.equal(turnState(props.run, [], false).actor, "可以阅读和试玩");
  assert.equal(props.run.run.status, "completed");
});

test("skill binding, dispatched instructions and received responses remain distinct claims", () => {
  const skill = { version: 1, steps: [{ status: "bound" }, { status: "unknown" }] };
  assert.equal(usageSummary(skill), "部分调用记录待核对");
  assert.equal(usageStatus("bound"), "已配置");
  skill.steps = [{ status: "response_received" }, { status: "dispatched" }, { status: "bound" }];
  assert.equal(usageSummary(skill), "2 个步骤带入调用，其中 1 个收到完整回应");
  assert.doesNotMatch(usageSummary(skill), /遵从|满意|质量|有效|成功/);
  assert.equal(usageSummary({ version: null, steps: [] }), "版本记录待核对");
});

function usageState(client) {
  const props = reactive({ project: "world", taskId: "task", run: runRecord(), events: [] });
  const env = { computed, props, usage: ref(null), loading: ref(false), error: ref(""), alive: true, generation: 0, sequence: 0, humanizeError: String, storyCreatorClient: () => client };
  return { ...env, ...execute(["contextKey", "readUsage"], env) };
}
const usageRecord = () => ({ project: "world", task_id: "task", run_id: "run", revision: 2, skills: [], coverage: "complete", semantic_compliance: "unknown" });

test("the lightweight usage response is bound to the exact task, run and revision", async () => {
  const state = usageState({ skillUsage: async () => usageRecord() });
  await state.readUsage();
  assert.equal(state.usage.value.run_id, "run");
  const wrong = usageState({ skillUsage: async () => ({ ...usageRecord(), revision: 1 }) });
  await wrong.readUsage();
  assert.equal(wrong.usage.value, null);
  assert.match(wrong.error.value, /不一致/);
});

test("an old run's delayed usage response cannot appear after a task switch", async () => {
  let resolve;
  const state = usageState({ skillUsage: () => new Promise(done => { resolve = done; }) });
  const pending = state.readUsage();
  state.props.taskId = "other-task";
  state.props.run = { ...runRecord(), run: { id: "other-run", revision: 1, status: "running" } };
  resolve(usageRecord());
  await pending;
  assert.equal(state.usage.value, null);
});

test("development task markers are explicit and unknown actor identity never becomes a human evaluation", () => {
  const props = reactive({ taskOrigin: "author" });
  const usage = ref({ source: { task_origin: "author", actor_origin: "unknown" } });
  const result = execute(["development"], { props, usage, computed });
  assert.equal(result.development.value, false);
  usage.value.source.task_origin = "development";
  assert.equal(result.development.value, true);
});
