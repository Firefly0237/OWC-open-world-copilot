import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const pages = Object.fromEntries(
  await Promise.all(
    [
      "ArchivePage",
      "AgentPage",
      "MultiAgentPage",
      "ReviewPage",
      "ExportPage",
      "AuditPage",
      "CompliancePage",
    ].map(async (name) => [
      name,
      await readFile(new URL(`../src/pages/${name}.vue`, import.meta.url), "utf8"),
    ]),
  ),
);

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

test("archive fetches bounded server pages and keeps cursor navigation in URL state", () => {
  const source = pages.ArchivePage;
  const load = extractFunction(source, "load");
  const previous = extractFunction(source, "goPrevious");
  const next = extractFunction(source, "goNext");
  assert.match(source, /const PAGE_SIZE = 20;/);
  for (const kind of [
    "entity",
    "quest",
    "region",
    "poi",
    "term",
    "dialogue",
    "dialogue_tree",
    "relation",
    "style_guide",
    "quest_event_ref",
    "style_rubric",
  ]) {
    assert.match(source, new RegExp(`kind:\\s*"${kind}"`), `${kind} must have a visible tab`);
  }
  assert.match(load, /kind: currentKind\.value/);
  assert.match(load, /limit: String\(PAGE_SIZE\)/);
  assert.match(load, /params\.set\("q", query\.value\.trim\(\)\)/);
  assert.match(load, /params\.set\("cursor", currentCursor\.value\)/);
  assert.match(load, /params\.set\("focus", focusRef\.value\)/);
  assert.match(load, /archive\?\$\{params\.toString\(\)\}/);
  assert.match(load, /body\.items/);
  assert.match(load, /body\.next_cursor/);
  assert.match(load, /body\.previous_cursor/);
  assert.match(load, /body\.page_cursor/);
  assert.match(load, /body\.offset/);
  assert.match(load, /page\.value = Math\.floor\(currentOffset\.value \/ PAGE_SIZE\) \+ 1/);
  assert.match(load, /body\.snapshot_etag/);
  assert.match(source, /const pagedItems = computed\(\(\) => currentItems\.value\)/);
  assert.doesNotMatch(source, /filtered\.value\.slice\(start, start \+ PAGE_SIZE\)/);
  assert.match(extractFunction(source, "selectKind"), /resetPaging\(\)/);
  assert.match(extractFunction(source, "syncRouteState"), /next\.page = String\(page\.value\)/);
  assert.match(extractFunction(source, "syncRouteState"), /next\.cursor = currentCursor\.value/);
  assert.match(previous, /previousCursor\.value \?\? known/);
  assert.match(previous, /currentCursor\.value = cursor/);
  assert.match(next, /currentCursor\.value = nextCursor\.value/);
  assert.match(next, /cursorTrail\.value\[page\.value\] = nextCursor\.value/);
  assert.match(source, /aria-label="档案分页"/);
  assert.match(source, /@click="goPrevious"/);
  assert.match(source, /@click="goNext"/);
});

test("agent page sends validated advanced controls and preserves the HITL resume protocol", () => {
  const source = pages.AgentPage;
  const run = extractFunction(source, "run");
  const decide = extractFunction(source, "decide");

  assert.match(source, /value !== "" && Number\.isInteger/);
  for (const key of [
    "max_steps",
    "hitl",
    "recall",
    "tool_transport",
    "early_stopping",
    "max_model_calls",
    "max_tool_calls",
    "max_cost_usd",
    "skills",
  ]) {
    assert.match(run, new RegExp(`\\b${key}\\b`), `${key} must reach the agent job`);
  }
  assert.match(run, /maxCostUsd\.value === "" \? \{\} : \{ max_cost_usd:/);
  assert.match(source, /<option value="mcp">MCP（仅 audit_project，真实 stdio）<\/option>/);
  assert.match(decide, /thread_id: thread/);
  assert.match(decide, /resume: true/);
  assert.match(decide, /decision_message/);
  assert.match(source, /result\.mcp_traces/);
  assert.match(source, /result\.run_budget/);
  assert.match(source, /result\.agent_run_id/);
});

test("multi-agent page submits bounded controls and routes persisted proposals to human review", () => {
  const source = pages.MultiAgentPage;
  const run = extractFunction(source, "run");

  assert.match(source, /getJobChannel<MultiAgentResult>\("multi_agent"/);
  assert.match(source, /value >= 1 && value <= 100/);
  assert.match(source, /value >= 1 && value <= 512/);
  assert.match(source, /value >= 1 && value <= 4096/);
  assert.match(run, /kind: "multi_agent"/);
  assert.match(run, /max_model_calls: modelLimit/);
  assert.match(run, /max_tool_calls: toolLimit/);
  assert.match(run, /max_total_tool_calls: totalToolLimit/);
  assert.match(run, /costThreshold == null \? \{\} : \{ max_cost_usd: costThreshold \}/);
  assert.match(source, /模型调用与会话工具调用各有一个全局硬账本/);
  assert.match(source, /已结算成本阻止下一次派发/);
  assert.match(source, /session_global_model_budget/);
  assert.match(source, /session_global_tool_budget/);
  assert.match(source, /offline_llm_allowed/);
  assert.match(source, /result\.blackboard_flow/);
  assert.match(source, /result\.verifier_verdicts/);
  assert.match(source, /result\.proposals\?\.length/);
  assert.match(source, /<RouterLink class="review-link" to="\/audit">/);
});

test("review calibration is read-only until explicit learning and learned lessons are revocable", () => {
  const source = pages.ReviewPage;
  const calibration = extractFunction(source, "loadCalibration");
  const learn = extractFunction(source, "learnFromCalibration");
  const revoke = extractFunction(source, "revokeLesson");

  assert.match(calibration, /apiGet<Calibration>/);
  assert.doesNotMatch(calibration, /apiPost/);
  assert.match(source, /`\/projects\/\$\{currentProject\(\)\}\/lessons\?limit=30`/);
  assert.match(learn, /review\/calibration:learn/);
  assert.match(learn, /await loadLessons\(\)/);
  assert.match(revoke, /lessons\/\$\{encodeURIComponent\(lesson\.id\)\}:revoke/);
  assert.match(revoke, /await loadLessons\(\)/);
  assert.match(source, /const cursor = nextLessonCursor\.value/);
  assert.match(source, /sequence !== lessonLoadSequence/);
  assert.match(source, /lessons\.value = \[\.\.\.lessons\.value, \.\.\.body\.lessons\]/);
  assert.match(source, /加载更多经验/);
  assert.match(source, /查看校准不会改动长期记忆/);
  assert.match(source, /撤销经验/);
});

test("engine roundtrip requires a non-empty baseline and reports every merge outcome", () => {
  const source = pages.ExportPage;
  const exportRun = extractFunction(source, "runExport");
  const importRun = extractFunction(source, "runImport");

  assert.match(exportRun, /planning_workbook: planningWorkbook\.value/);
  assert.match(exportRun, /planning_notes: planningNotes\.value/);
  assert.match(importRun, /JSON\.parse\(baselineText\.value\)/);
  assert.match(importRun, /!Array\.isArray\(baselineQuests\) \|\| baselineQuests\.length === 0/);
  assert.match(importRun, /baseline_quests: baselineQuests/);
  assert.match(source, /:disabled="importing \|\| !importText\.trim\(\) \|\| !baselineText\.trim\(\)"/);
  for (const outcome of ["conflicts", "library_ahead", "engine_missing", "warnings"]) {
    assert.match(source, new RegExp(`importPlan\\.${outcome}`), `${outcome} must be disclosed`);
  }
});

test("review, audit, and compliance lists keep following opaque server cursors", () => {
  const review = pages.ReviewPage;
  assert.match(review, /review_items\?limit=50/);
  assert.match(review, /const cursor = nextQueueCursor\.value/);
  assert.match(review, /new URLSearchParams\(\{ limit: "50", cursor \}\)/);
  assert.match(review, /sequence !== queueLoadSequence/);
  assert.match(review, /items\.value = \[\.\.\.items\.value, \.\.\.body\.items\]/);
  assert.match(review, /nextQueueCursor\.value = body\.next_cursor/);
  assert.match(review, /v-if="nextQueueCursor"[\s\S]*@click="loadMoreItems"/);

  const audit = pages.AuditPage;
  assert.match(audit, /new URLSearchParams\(\{ limit: "50" \}\)/);
  assert.match(audit, /query\.set\("severity", filter\.value\)/);
  assert.match(audit, /query\.set\("cursor", nextIssuesCursor\.value\)/);
  assert.match(audit, /sequence !== issuesLoadSequence/);
  assert.match(audit, /issues\.value = append \? \[\.\.\.issues\.value, \.\.\.body\.issues\]/);
  assert.match(audit, /v-if="nextIssuesCursor"[\s\S]*@click="loadIssues\(true\)"/);

  const compliance = pages.CompliancePage;
  assert.match(compliance, /query\.set\("status", statusFilter\.value\)/);
  assert.match(compliance, /query\.set\("assignee", assigneeFilter\.value\.trim\(\)\)/);
  assert.match(compliance, /query\.set\("cursor", nextCaseCursor\.value\)/);
  assert.match(compliance, /sequence !== loadSequence/);
  assert.match(compliance, /cases: \[\.\.\.report\.value\.cases, \.\.\.body\.report\.cases\]/);
  assert.match(compliance, /v-if="nextCaseCursor"[\s\S]*@click="load\(true\)"/);
});

test("applied patches remain discoverable and rollbackable after a fresh audit", () => {
  const audit = pages.AuditPage;
  const loadApplied = extractFunction(audit, "loadAppliedPatches");
  const runAudit = extractFunction(audit, "runAudit");

  assert.match(loadApplied, /status: "applied"/);
  assert.match(loadApplied, /limit: "20"/);
  assert.match(loadApplied, /nextAppliedPatchesCursor\.value/);
  assert.match(runAudit, /await loadAppliedPatches\(\)/);
  assert.match(audit, /已应用修复 · 可回滚/);
  assert.match(audit, /@click="rollback\(candidate\)"/);
  assert.match(audit, /@click="loadAppliedPatches\(true\)"/);
});

test("archive exposes media attachment for every supported object kind", () => {
  const archive = pages.ArchivePage;
  const open = extractFunction(archive, "openItem");
  const attach = extractFunction(archive, "attachAsset");
  const detach = extractFunction(archive, "detachAsset");

  assert.match(open, /void loadAssets\(item\.ref\)/);
  assert.doesNotMatch(attach, /item\.kind !== "entity"/);
  assert.doesNotMatch(detach, /item\.kind !== "entity"/);
  assert.match(archive, /<div class="section sub"><span class="t">关联素材<\/span><\/div>/);
});

test("archive exposes one collaboration ledger for every canon detail", () => {
  const archive = pages.ArchivePage;
  const open = extractFunction(archive, "openItem");
  const loadCollab = extractFunction(archive, "loadCollab");
  const assign = extractFunction(archive, "assignToMe");
  const comment = extractFunction(archive, "postComment");
  const lock = extractFunction(archive, "setEditLock");
  const heartbeat = extractFunction(archive, "syncLockHeartbeat");
  const reset = extractFunction(archive, "resetDetailState");

  assert.match(open, /loadCollab\(item\.ref\)/);
  assert.match(loadCollab, /collab\?object_ref=\$\{encodeURIComponent\(objectRef\)\}/);
  assert.match(loadCollab, /limit=\$\{COLLAB_PAGE_SIZE\}/);
  assert.match(loadCollab, /cursor=\$\{encodeURIComponent\(cursor\)\}/);
  assert.match(loadCollab, /append && error instanceof ApiError && error\.status === 409/);
  assert.match(loadCollab, /await loadCollab\(objectRef\)/);
  assert.ok(
    (loadCollab.match(/activeObjectRef\.value === objectRef && collabRequestSerial === requestSerial/g) ?? [])
      .length >= 3,
    "stale collaboration success, error, and loading state must all be detail guarded",
  );
  assert.match(assign, /collab\/assign/);
  assert.match(assign, /object_ref: objectRef/);
  assert.match(comment, /collab\/comments/);
  assert.match(comment, /object_ref: objectRef/);
  assert.ok(
    lock.indexOf("lockDesired.set(objectRef, !release)") < lock.indexOf("queueLockRequest(objectRef"),
    "release intent must fence queued heartbeats",
  );
  assert.match(lock, /collab\/lock/);
  assert.match(heartbeat, /45_000/);
  assert.ok(
    (heartbeat.match(/lockDesired\.get\(objectRef\) !== true/g) ?? []).length >= 2,
    "heartbeat must re-check release intent before queueing and posting",
  );
  assert.match(reset, /releaseDetailLock\(objectRef\)/);
  for (const mutation of ["attachAsset", "detachAsset", "save", "remove"]) {
    assert.match(
      extractFunction(archive, mutation),
      /ensureOwnLock\(/,
      `${mutation} must honor the selected object's soft lock`,
    );
  }
  const panelTag = archive.split("\n").find((line) => line.includes("data-collab-object-detail")) ?? "";
  assert.doesNotMatch(panelTag, /v-if=/, "the collaboration panel must not be restricted to one kind");
  assert.match(archive, /负责人/);
  assert.match(archive, /collaborationStatus/);
  assert.match(archive, /释放编辑锁/);
  assert.match(archive, /加载更早评论/);
  assert.match(archive, /@click="loadCollab\(activeObjectRef, true\)"/);
});

test("dialogue trees can be deleted as whole canon objects from the archive", () => {
  const archive = pages.ArchivePage;
  const remove = extractFunction(archive, "remove");

  assert.match(archive, /detail\.value\?\.kind === "dialogue_tree"/);
  assert.match(remove, /objects\/\$\{item\.kind\}\/\$\{encodeURIComponent\(item\.id\)\}/);
  assert.match(archive, /删除整棵树/);
});

test("compliance scan and rescan share a validated custom rule pack", () => {
  const source = pages.CompliancePage;
  const policy = extractFunction(source, "scanPolicy");
  const modelPolicy = extractFunction(source, "complianceModelPolicy");
  const scan = extractFunction(source, "scan");
  const rescan = extractFunction(source, "rescan");

  assert.match(policy, /JSON\.parse\(source\)/);
  assert.match(policy, /rule_pack: parsed/);
  assert.match(modelPolicy, /llmConfig\(\)\.ready \? llmParams\(\) : \{ llm_mode: "offline" \}/);
  assert.match(policy, /\.\.\.complianceModelPolicy\(\)/);
  assert.match(scan, /scanPolicy\(\)/);
  assert.match(rescan, /operator: operator\.value\.trim\(\), \.\.\.scanPolicy\(\)/);
  assert.match(source, /自定义合规规则包 JSON/);
});
