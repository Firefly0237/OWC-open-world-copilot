import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const [page, domain] = await Promise.all([
  readFile(new URL("../src/pages/LocalizationPage.vue", import.meta.url), "utf8"),
  readFile(new URL("../../../src/owcopilot/localization.py", import.meta.url), "utf8"),
]);

const TRANSITIONS = [
  ["untranslated", "translated"],
  ["translated", "reviewing"],
  ["translated", "untranslated"],
  ["reviewing", "final"],
  ["reviewing", "translated"],
  ["final", "reviewing"],
];

test("localization UI exposes every transition allowed by the domain state machine", () => {
  const uiFlow = /const TRANSITIONS:[\s\S]*?= \{([\s\S]*?)\n\};/.exec(page)?.[1] ?? "";
  for (const [from, to] of TRANSITIONS) {
    assert.match(domain, new RegExp(`LocStatus\\.${from.toUpperCase()}[\\s\\S]*?LocStatus\\.${to.toUpperCase()}`));
    const row = new RegExp(`${from}: \\[([\\s\\S]*?)\\n  \\]`).exec(uiFlow)?.[1]
      ?? new RegExp(`${from}: \\[([^\\]]*)\\]`).exec(uiFlow)?.[1]
      ?? "";
    assert.match(row, new RegExp(`to: "${to}"`), `${from} -> ${to} is missing from the UI`);
  }
});

test("localization transition buttons send the selected target state", () => {
  assert.match(page, /async function transition\(key: string, locale: string, to: string\)/);
  assert.match(page, /localization:transition[\s\S]*?text_key: key,[\s\S]*?locale,[\s\S]*?to,[\s\S]*?by,/);
  assert.match(page, /v-for="action in TRANSITIONS\[row\.status\[locale\]\] \?\? \[\]"/);
  assert.match(page, /@click="transition\(row\.text_key, locale, action\.to\)"/);
});
