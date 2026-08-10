import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";

const root = path.resolve(import.meta.dirname, "..");

function source(relativePath) {
  return fs.readFileSync(path.join(root, relativePath), "utf8");
}

test("impact analysis renders bounded must-change and review pages", () => {
  const page = source("src/pages/ImpactPage.vue");
  assert.match(page, /const RESULT_PAGE_SIZE = 50/);
  assert.match(page, /v-for="\(it, i\) in visibleMust"/);
  assert.match(page, /v-for="\(it, i\) in visibleSuggest"/);
  assert.match(page, /visibleMustCount \+= RESULT_PAGE_SIZE/);
  assert.match(page, /visibleSuggestCount \+= RESULT_PAGE_SIZE/);
});

test("contradiction scan renders bounded confirmed and review pages", () => {
  const page = source("src/pages/AuditPage.vue");
  assert.match(page, /const CONTRA_PAGE_SIZE = 50/);
  assert.match(page, /in visibleContradictions/);
  assert.match(page, /in visibleContraReviews/);
  assert.match(page, /visibleContradictionCount \+= CONTRA_PAGE_SIZE/);
  assert.match(page, /visibleReviewCount \+= CONTRA_PAGE_SIZE/);
});

test("theme sweep renders a bounded combined findings page", () => {
  const page = source("src/pages/SweepPage.vue");
  assert.match(page, /const FINDING_PAGE_SIZE = 50/);
  assert.match(page, /v-for="finding in visibleFindings"/);
  assert.match(page, /visibleFindingCount \+= FINDING_PAGE_SIZE/);
});
