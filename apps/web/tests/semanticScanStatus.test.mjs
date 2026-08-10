import assert from "node:assert/strict";
import test from "node:test";

import {
  failedSemanticScan,
  SEMANTIC_STATUS_MESSAGE,
  semanticScanStatus,
} from "../src/semanticScanStatus.ts";

function result(overrides = {}) {
  return {
    contradictions: [],
    review_suggested: [],
    candidate_count: 1,
    judged_count: 0,
    judge_skipped: 0,
    semantic_used: true,
    llm_used: false,
    ...overrides,
  };
}

test("semantic degrade is NOT_AVAILABLE even if deterministic candidates exist", () => {
  assert.equal(semanticScanStatus(result({ semantic_used: false })), "NOT_AVAILABLE");
});

test("zero semantic candidates is NO_CANDIDATE, never CLEAN", () => {
  const status = semanticScanStatus(result({ candidate_count: 0 }));
  assert.equal(status, "NO_CANDIDATE");
  assert.doesNotMatch(SEMANTIC_STATUS_MESSAGE[status], /干净/);
});

test("current protocol cannot claim CLEAN from an empty finding list", () => {
  assert.equal(
    semanticScanStatus(result({ candidate_count: 2, judged_count: 2 })),
    "INCOMPLETE",
  );
});

test("CLEAN requires candidates and an explicit complete-exclusion attestation", () => {
  assert.equal(
    semanticScanStatus(
      result({
        candidate_count: 2,
        judged_count: 2,
        protocol_complete_exclusion: true,
      }),
    ),
    "CLEAN",
  );
});

test("reported findings remain RISK", () => {
  assert.equal(
    semanticScanStatus(result({ review_suggested: [{ point: "冲突候选" }] })),
    "RISK",
  );
});

test("ERROR allocates empty state instead of retaining a previous result", () => {
  const failed = failedSemanticScan();
  assert.equal(failed.status, "ERROR");
  assert.deepEqual(failed.result.contradictions, []);
  assert.deepEqual(failed.result.review_suggested, []);
  assert.equal(failed.result.candidate_count, 0);
});
