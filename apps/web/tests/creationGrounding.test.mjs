import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const source = await readFile(
  new URL("../src/pages/CreationPage.vue", import.meta.url),
  "utf8",
);

function extractFunction(name) {
  const match = new RegExp(`async\\s+function\\s+${name}\\s*\\(`).exec(source);
  assert.ok(match, `${name} must exist`);
  const open = source.indexOf("{", match.index);
  let depth = 0;
  for (let index = open; index < source.length; index += 1) {
    if (source[index] === "{") depth += 1;
    if (source[index] === "}") depth -= 1;
    if (depth === 0) return source.slice(match.index, index + 1);
  }
  throw new Error(`Could not find the end of ${name}`);
}

test("every creation chain exposes grounding, provenance, and refine controls", () => {
  for (const [name, prefix] of [
    ["runDraft", "draft"],
    ["runDialogue", "dlg"],
    ["runBarks", "bark"],
    ["runFlavor", "flavor"],
  ]) {
    const body = extractFunction(name);
    assert.match(body, new RegExp(`use_references:\\s*${prefix}UseReferences\\.value`));
    assert.match(body, new RegExp(`refine_rounds:\\s*${prefix}RefineRounds\\.value`));
    assert.match(body, /inspiration_refs/);
    assert.match(body, /reference_report/);
    assert.match(source, new RegExp(`v-model="${prefix}UseReferences"`));
    assert.match(source, new RegExp(`v-model\\.number="${prefix}RefineRounds"`));
  }
  assert.match(source, /row\.leakage\?\.message/);
  assert.match(source, /row\.leakage\.detected/);
});

test("creation character pickers follow bounded archive cursors", () => {
  const load = extractFunction("loadNpcs");
  assert.match(load, /kind: "entity", limit: String\(NPC_PAGE_SIZE\)/);
  assert.match(load, /params\.set\("cursor", cursor\)/);
  assert.match(load, /body\.items\.filter/);
  assert.match(load, /body\.next_cursor/);
  assert.match(load, /e instanceof ApiError && e\.status === 409/);
  assert.match(source, /@click="loadMoreNpcs"/);
  assert.doesNotMatch(source, /apiGet<\{ inventory: \{ entities: ArchiveEntity\[\] \} \}>/);
});

test("creation forwards the bounded dialogue, bark, and flavor controls", () => {
  const dialogue = extractFunction("runDialogue");
  assert.match(dialogue, /quest_id:\s*dlgQuestId\.value\.trim\(\) \|\| null/);
  assert.match(dialogue, /max_nodes:\s*dlgMaxNodes\.value/);
  assert.match(dialogue, /max_chars:\s*dlgMaxChars\.value/);

  const barks = extractFunction("runBarks");
  assert.match(barks, /max_chars:\s*barkMaxChars\.value/);
  assert.match(barks, /allowed_entity_ids:\s*parseIdList\(barkAllowedEntityIds\.value\)/);

  const flavor = extractFunction("runFlavor");
  assert.match(flavor, /max_chars:\s*flavorMaxChars\.value/);

  for (const model of [
    "dlgQuestId",
    "dlgMaxNodes",
    "dlgMaxChars",
    "barkMaxChars",
    "barkAllowedEntityIds",
    "flavorMaxChars",
  ]) {
    assert.match(source, new RegExp(`v-model(?:\\.number)?="${model}"`));
  }
});
