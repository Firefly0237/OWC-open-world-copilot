import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "typescript";

const source = await readFile(new URL("../src/pages/TimelinePage.vue", import.meta.url), "utf8");

function extractFunction(name) {
  const match = new RegExp(`function\\s+${name}(?:<[^>]+>)?`).exec(source);
  assert.ok(match, `TimelinePage must define ${name}`);
  const start = match.index;
  const open = source.indexOf("{", start);
  let depth = 0;
  for (let index = open; index < source.length; index += 1) {
    if (source[index] === "{") depth += 1;
    if (source[index] === "}") depth -= 1;
    if (depth === 0) return source.slice(start, index + 1);
  }
  throw new Error(`Could not find the end of ${name}`);
}

const helpers = ts.transpileModule(
  `${extractFunction("selectorList")}\n${extractFunction("parseInitialState")}\nexport { selectorList, parseInitialState };`,
  { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } },
).outputText;
const { selectorList, parseInitialState } = await import(
  `data:text/javascript,${encodeURIComponent(helpers)}`
);

test("timeline playtest parses selectors and scalar initial state before submission", () => {
  assert.deepEqual(selectorList("branch_a， branch_b\nbranch_c"), [
    "branch_a",
    "branch_b",
    "branch_c",
  ]);
  assert.deepEqual(parseInitialState('{"flag":true,"count":2,"name":"Mara"}'), {
    flag: true,
    count: 2,
    name: "Mara",
  });
  assert.throws(() => parseInitialState("[]"), /JSON 对象/);
  assert.throws(() => parseInitialState('{"ratio":1.5}'), /布尔值、整数或字符串/);
  assert.throws(() => parseInitialState('{"nested":{"bad":true}}'), /布尔值、整数或字符串/);
});

test("timeline playtest sends and renders the complete dialogue-to-quest contract", () => {
  const run = extractFunction("runSim");
  for (const field of [
    "choices",
    "initial_state",
    "dialogue_tree_id",
    "dialogue_choices",
  ]) {
    assert.match(run, new RegExp(field));
  }
  assert.match(source, /sim\?\.dialogue_walk/);
  assert.match(source, /sim\.dialogue_walk\.steps/);
  assert.match(source, /sim\.run\.steps/);
  assert.ok((source.match(/formatState\(/g) ?? []).length >= 3);
  assert.match(source, /初始变量在同名键上优先/);
  assert.match(source, /:disabled="simBusy"/);
});
