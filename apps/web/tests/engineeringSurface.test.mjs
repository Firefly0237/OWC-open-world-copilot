import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const page = await readFile(
  new URL("../src/pages/EngineeringPage.vue", import.meta.url),
  "utf8",
);
const router = await readFile(new URL("../src/router.ts", import.meta.url), "utf8");
const shell = await readFile(new URL("../src/App.vue", import.meta.url), "utf8");

function extractFunction(source, name) {
  const match = new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\(`).exec(source);
  assert.ok(match, `${name} must exist`);
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

test("engineering lab is reachable from the production router and navigation", () => {
  assert.match(router, /path: "\/engineering"[\s\S]*EngineeringPage\.vue/);
  assert.match(shell, /to: "\/engineering", label: "工程实验室"/);
  assert.match(page, /overline="ENGINEERING LAB"/);
  for (const label of [
    "任务穷举",
    "规约编写",
    "对话游走",
    "旗标引用",
    "MCTS 修复",
    "风格清查",
    "质量闭环",
    "运行历史",
  ]) {
    assert.match(page, new RegExp(label));
  }
});

test("every former CLI-only engineering capability calls its typed FastAPI route", () => {
  const expectations = {
    runQuestExplore: /quests\/\$\{encodeURIComponent\(questId\.value\)\}:explore/,
    runDialogueWalk: /dialogues\/\$\{encodeURIComponent\(dialogueId\.value\)\}:walk/,
    runFlagXref: /flags:xref/,
    runRepairPlan: /repairs:plan/,
    runStyleSweep: /styles\/\$\{encodeURIComponent\(rubricId\.value\)\}:sweep/,
    runQualityHarness: /quality:run/,
    runSpecTranslate: /quests\/\$\{encodeURIComponent\(selectedQuest\)\}\/specs:translate/,
  };
  for (const [name, pattern] of Object.entries(expectations)) {
    assert.match(extractFunction(page, name), pattern, `${name} must use the service API`);
  }
  assert.match(extractFunction(page, "loadCatalog"), /\/catalog/);
  assert.match(page, /规约与修复只能先送审，任何工具都不会在这里直接修改正典/);
  assert.match(page, /前往「校勘修复」人工审阅/);
});

test("temporal spec authoring exposes every checkable step and a separate review gate", () => {
  const translate = extractFunction(page, "runSpecTranslate");
  const submit = extractFunction(page, "submitTemporalSpec");

  assert.match(translate, /use_llm: specUseModel\.value/);
  assert.match(translate, /specUseModel\.value \? llmParams\(\) : \{ llm_mode: "offline" \}/);
  assert.match(translate, /addSessionCost\(costOf\(body\)\)/);
  assert.match(submit, /outcome\.status !== "needs_confirmation"/);
  assert.match(submit, /intent: translated\.outcome\.intent/);
  assert.match(submit, /draft: translated\.outcome\.draft/);
  assert.match(submit, /specs:submit/);

  for (const evidence of [
    "受限结构草稿",
    "封闭世界旗标接地",
    "确定性组装与类型检查",
    "回译确认",
    "人工送审",
    "确认回译并送审",
    "正典仍未写入",
  ]) {
    assert.match(page, new RegExp(evidence));
  }
  assert.match(page, /specResult\.translator_mode/);
  assert.match(page, /specResult\.degraded/);
  assert.match(page, /to="\/review"/);
});

test("agent history follows opaque server cursors and recovers from stale snapshots", () => {
  const load = extractFunction(page, "loadAgentRuns");
  const next = extractFunction(page, "nextAgentRuns");
  const previous = extractFunction(page, "previousAgentRuns");

  assert.match(load, /limit: String\(HISTORY_PAGE_SIZE\)/);
  assert.match(load, /query\.set\("cursor", cursor\)/);
  assert.match(load, /body\.next_cursor/);
  assert.match(load, /body\.previous_cursor/);
  assert.match(load, /body\.page_cursor/);
  assert.match(load, /body\.snapshot_etag/);
  assert.match(load, /error instanceof ApiError && error\.status === 409/);
  assert.match(load, /loadAgentRuns\(null, 1, false\)/);
  assert.match(next, /nextRunCursor\.value/);
  assert.match(next, /runCursorTrail\.value\[runPage\.value\] = cursor/);
  assert.match(previous, /previousRunCursor\.value \?\? known/);
  assert.match(page, /aria-label="Agent 运行历史分页"/);
  assert.match(page, /@click="previousAgentRuns"/);
  assert.match(page, /@click="nextAgentRuns"/);
  assert.match(extractFunction(page, "openAgentRun"), /agent-runs\/\$\{encodeURIComponent\(runId\)\}/);
});

test("bounded controls expose honest model-assisted and proposal-only semantics", () => {
  assert.match(page, /max="1000000"/);
  assert.match(page, /max="10000"/);
  assert.match(page, /max="2000"/);
  assert.match(page, /v-model="repairUseModel"[\s\S]*:disabled="!llmReady"/);
  assert.match(page, /v-model\.number="repairMaxModelCalls"[\s\S]*max="128"/);
  assert.match(page, /v-model="styleUseJudge"[\s\S]*:disabled="!llmReady"/);
  assert.match(page, /v-model\.number="styleMaxJudge"[\s\S]*max="2000"/);
  assert.match(page, /to="\/settings"/);

  const repair = extractFunction(page, "runRepairPlan");
  assert.match(repair, /llm_candidates: repairUseModel\.value/);
  assert.match(repair, /max_llm_calls: repairMaxModelCalls\.value/);
  assert.match(repair, /repairUseModel\.value \? llmParams\(\) : \{ llm_mode: "offline" \}/);
  assert.match(repair, /max_cost_usd: Number\(repairMaxCostUsd\.value\)/);

  const style = extractFunction(page, "runStyleSweep");
  assert.match(style, /use_llm: styleUseJudge\.value/);
  assert.match(style, /max_judge: styleMaxJudge\.value/);
  assert.match(style, /styleUseJudge\.value \? llmParams\(\) : \{ llm_mode: "offline" \}/);
  assert.match(style, /max_cost_usd: Number\(styleMaxCostUsd\.value\)/);

  assert.match(page, /candidate_source/);
  assert.match(page, /repairResult\.used_llm/);
  assert.match(page, /styleResult\.llm_used/);
  assert.match(page, /telemetry\.calls/);
  assert.match(page, /cost_budget\.used_usd/);
  assert.match(page, /模型调用硬上限/);
  assert.match(page, /成本值是运行后结算阈值/);
  assert.match(page, /未写正典/);
  assert.match(page, /propose_fixes: proposeFixes\.value/);
  assert.match(page, /max_candidates_per_issue: qualityCandidates\.value/);
});
