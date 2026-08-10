import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "typescript";

const [
  api,
  agent,
  multiAgent,
  help,
  guidedTour,
  commandPalette,
  templates,
  canonical,
  intake,
  settings,
  chatModel,
] = await Promise.all([
  readFile(new URL("../src/api.ts", import.meta.url), "utf8"),
  readFile(new URL("../src/pages/AgentPage.vue", import.meta.url), "utf8"),
  readFile(new URL("../src/pages/MultiAgentPage.vue", import.meta.url), "utf8"),
  readFile(new URL("../src/pageHelp.ts", import.meta.url), "utf8"),
  readFile(new URL("../src/components/GuidedTour.vue", import.meta.url), "utf8"),
  readFile(new URL("../src/components/CommandPalette.vue", import.meta.url), "utf8"),
  readFile(new URL("../src/pages/TemplatesPage.vue", import.meta.url), "utf8"),
  readFile(new URL("../src/pages/CanonicalPage.vue", import.meta.url), "utf8"),
  readFile(new URL("../src/pages/IntakePage.vue", import.meta.url), "utf8"),
  readFile(new URL("../src/pages/SettingsPage.vue", import.meta.url), "utf8"),
  readFile(new URL("../../../src/owcopilot/llm/chat_model.py", import.meta.url), "utf8"),
]);

function extractFunction(source, name) {
  const match = new RegExp(`(?:export\\s+)?function\\s+${name}\\s*\\(`).exec(source);
  assert.ok(match, `${name} must be defined`);
  const open = source.indexOf("{", match.index);
  let depth = 0;
  for (let index = open; index < source.length; index += 1) {
    if (source[index] === "{") depth += 1;
    if (source[index] === "}") depth -= 1;
    if (depth === 0) return source.slice(match.index, index + 1);
  }
  throw new Error(`Could not find the end of ${name}`);
}

function timeoutClassifier() {
  const constants = api.match(/export const REQUEST_TIMEOUT_MS = Object\.freeze\([\s\S]*?\n\}\);/)?.[0];
  assert.ok(constants, "timeout constants must be declared");
  const source = `${constants}\n${extractFunction(api, "defaultRequestTimeoutMs")}`.replaceAll("export ", "");
  const output = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.None, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  return new Function(`${output}\nreturn { REQUEST_TIMEOUT_MS, defaultRequestTimeoutMs };`)();
}

test("synchronous AI calls outlive their backend provider deadlines and remain overrideable", () => {
  const { REQUEST_TIMEOUT_MS: timeout, defaultRequestTimeoutMs: classify } = timeoutClassifier();
  assert.equal(classify("/projects/demo/extractions:run"), timeout.AI_EXTRACTION);
  assert.equal(classify("/projects/demo/assist/dialogue_trees:draft"), timeout.AI_LONG_GENERATION);
  assert.equal(classify("/projects/demo/contents/quests:draft"), timeout.AI_GENERATION);
  assert.equal(classify("/projects/demo/ask"), timeout.AI_DEFAULT);
  assert.equal(classify("/projects/demo/compliance/cases/42:rescan"), timeout.AI_DEFAULT);
  assert.equal(classify("/projects/demo/jobs"), timeout.DEFAULT);
  assert.ok(timeout.AI_DEFAULT > 60_000);
  assert.ok(timeout.AI_GENERATION > 90_000);
  assert.ok(timeout.AI_LONG_GENERATION > 120_000);
  assert.ok(timeout.AI_EXTRACTION > 240_000);
  assert.match(api, /options\.timeoutMs/);
  assert.match(api, /options\.timeoutMs \?\? REQUEST_TIMEOUT_MS\.FILE_TRANSFER/);
});

test("team errors preserve structured codes and do not redirect auth or conflicts to model setup", () => {
  assert.match(api, /public readonly code = ""/);
  assert.match(api, /public readonly domain: "local" \| "team" \| "platform"/);
  assert.match(api, /body\.detail && typeof body\.detail === "object"/);
  assert.match(api, /throw new ApiError\(response\.status, detail\.slice\(0, 300\), code, domain\)/);
  const teamBranch = api.slice(api.indexOf('e.domain === "team"'), api.indexOf('e.domain === "platform"'));
  for (const status of [401, 403, 409, 413]) assert.match(teamBranch, new RegExp(`status === ${status}`));
  assert.doesNotMatch(teamBranch, /模型|设置/);
  assert.match(api, /ensureOk\(response, "team"\)/);
});

test("agent surfaces expose offline doubles only when the backend explicitly allows them", () => {
  for (const source of [agent, multiAgent]) {
    assert.match(source, /apiGet<\{ offline_llm_allowed\?: boolean \}>\("\/health"\)/);
    assert.match(source, /offlineAllowed\.value = health\.offline_llm_allowed === true/);
    assert.match(source, /<option v-if="offlineAllowed" value="offline">离线测试（开发 \/ CI · \$0）<\/option>/);
    assert.match(source, /removeEventListener\("ow-llm-changed", onLlmChanged\)/);
  }
  assert.match(agent, /to="\/audit"/);
  assert.match(help, /"\/multi-agent":\s*\{/);
  assert.match(help, /"\/engineering":\s*\{/);
});

test("modal-like surfaces trap and restore focus, and templates reject incomplete input", () => {
  for (const source of [guidedTour, commandPalette]) {
    assert.match(source, /role="dialog"/);
    assert.match(source, /aria-modal="true"/);
    assert.match(source, /e\.key === "Tab"/);
    assert.match(source, /returnFocus/);
  }
  assert.match(templates, /missingRequired = computed/);
  assert.match(templates, /:required="p\.required"/);
  assert.match(templates, /:aria-invalid=/);
  assert.match(templates, /busy \|\| missingRequired\.length > 0/);
  assert.match(templates, /v-else-if="loadError"/);
  assert.match(templates, /v-else-if="!templates\.length"/);
});

test("team intake and canonical views use bounded cursor pages with stale-page recovery", () => {
  for (const source of [canonical, intake]) {
    assert.match(source, /PAGE_SIZE/);
    assert.match(source, /cursor/);
    assert.match(source, /(?:pageRequestSerial|requestSerial)/);
    assert.match(source, /instanceof ApiError && .*status === 409/);
  }
  assert.match(canonical, /下载完整快照/);
  assert.match(canonical, /pagination\.next_cursor/);
  assert.match(intake, /allSegmentsLoaded/);
  assert.match(intake, /服务端每页最多/);
});

test("Anthropic preset is backed by the native LangChain adapter", () => {
  assert.match(settings, /provider: "anthropic"/);
  assert.match(settings, /base_url: "https:\/\/api\.anthropic\.com"/);
  assert.match(settings, /provider: PRESETS\[form\.provider\]\.provider/);
  assert.match(chatModel, /from langchain_anthropic import ChatAnthropic/);
  assert.match(chatModel, /resolved_provider == "anthropic"/);
});
