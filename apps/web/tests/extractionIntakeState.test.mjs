import assert from "node:assert/strict";
import test from "node:test";

import {
  bindExtractionIntake,
  extractionIntakeReady,
  invalidateExtractionIntake,
} from "../src/extractionIntakeState.ts";

const draft = { id: "draft-1" };

test("accepted provenance and token are persisted as one ready binding", () => {
  const state = bindExtractionIntake({
    draft,
    intakeToken: "signed-token",
    responseProvenance: "licensed",
    requestedProvenance: "licensed",
    selectedProvenance: "licensed",
  });

  assert.equal(state.draft, draft);
  assert.equal(state.intakeToken, "signed-token");
  assert.equal(state.acceptedProvenance, "licensed");
  assert.equal(state.ready, true);
  assert.equal(extractionIntakeReady(state, "licensed"), true);
});

test("missing response provenance falls back to the immutable request snapshot", () => {
  const accepted = bindExtractionIntake({
    draft,
    intakeToken: "signed-token",
    requestedProvenance: "own_ip",
    selectedProvenance: "own_ip",
  });
  assert.equal(accepted.acceptedProvenance, "own_ip");
  assert.equal(accepted.ready, true);

  const changedWhileRunning = bindExtractionIntake({
    draft,
    intakeToken: "signed-token",
    requestedProvenance: "own_ip",
    selectedProvenance: "licensed",
  });
  assert.deepEqual(changedWhileRunning, invalidateExtractionIntake());
});

test("an echoed declaration differing from request or current selection fails closed", () => {
  const mismatchedResponse = bindExtractionIntake({
    draft,
    intakeToken: "signed-token",
    responseProvenance: "licensed",
    requestedProvenance: "own_ip",
    selectedProvenance: "own_ip",
  });
  assert.deepEqual(mismatchedResponse, invalidateExtractionIntake());

  const invalidResponse = bindExtractionIntake({
    draft,
    intakeToken: "signed-token",
    responseProvenance: "third_party_reference",
    requestedProvenance: "own_ip",
    selectedProvenance: "own_ip",
  });
  assert.deepEqual(invalidResponse, invalidateExtractionIntake());
});

test("changing provenance invalidates draft, token, accepted provenance, and ready state", () => {
  const cleared = invalidateExtractionIntake();
  assert.equal(cleared.draft, null);
  assert.equal(cleared.intakeToken, "");
  assert.equal(cleared.acceptedProvenance, null);
  assert.equal(cleared.ready, false);
  assert.equal(extractionIntakeReady(cleared, "licensed"), false);
});

test("submit readiness rechecks the current declaration even for a populated state", () => {
  const state = bindExtractionIntake({
    draft,
    intakeToken: "signed-token",
    responseProvenance: "own_ip",
    requestedProvenance: "own_ip",
    selectedProvenance: "own_ip",
  });
  assert.equal(extractionIntakeReady(state, "licensed"), false);
});
