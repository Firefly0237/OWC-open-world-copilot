import assert from "node:assert/strict";
import test from "node:test";

import {
  allowedLineageKindsForDecision,
  buildTeamIntakeUploadPath,
  canCreateIntakeLineage,
  initialDraftingPreflight,
  isIntakeReadOnly,
} from "../src/teamIntakeState.ts";

test("replacement upload carries only an explicitly supplied artifact identity", () => {
  const created = buildTeamIntakeUploadPath("container/id", "source one.docx");
  const replaced = buildTeamIntakeUploadPath(
    "container/id",
    "source two.docx",
    "  artifact-v1  ",
  );

  assert.equal(
    created,
    "/team/containers/container%2Fid/intake-runs?filename=source+one.docx",
  );
  assert.equal(
    replaced,
    "/team/containers/container%2Fid/intake-runs?filename=source+two.docx&artifact_id=artifact-v1",
  );
});

test("submitted active runs allow lineage but unsubmitted runs do not", () => {
  assert.equal(canCreateIntakeLineage({ submitted: true }), true);
  assert.equal(canCreateIntakeLineage({ submitted: false }), false);
});

test("lineage kind follows the final human role and rejects unusable decisions", () => {
  assert.deepEqual(
    allowedLineageKindsForDecision({ decision: "CONFIRM", content_role: "CANON_CANDIDATE" }),
    ["SOURCE_EVIDENCE"],
  );
  assert.deepEqual(
    allowedLineageKindsForDecision({ decision: "OVERRIDE", content_role: "REFERENCE" }),
    ["INSPIRATION_LINEAGE"],
  );
  assert.deepEqual(
    allowedLineageKindsForDecision({ decision: "REJECT", content_role: "CANON_CANDIDATE" }),
    [],
  );
  assert.deepEqual(
    allowedLineageKindsForDecision({ decision: "OVERRIDE", content_role: "UNKNOWN" }),
    [],
  );
});

test("logical deletion is always read-only and fail-closes drafting and lineage", () => {
  const deleted = {
    submitted: true,
    logical_deleted: true,
    drafting_allowed: true,
  };
  const deletedByArtifact = {
    submitted: true,
    artifact: { logical_deleted_at: "2026-08-04T12:00:00Z" },
  };

  assert.equal(isIntakeReadOnly(deleted), true);
  assert.equal(isIntakeReadOnly(deletedByArtifact), true);
  assert.equal(canCreateIntakeLineage(deleted), false);
  assert.equal(initialDraftingPreflight(deleted), "denied");
  assert.equal(initialDraftingPreflight(deletedByArtifact), "denied");
});

test("active runs require an explicit drafting preflight even with a stored grant", () => {
  assert.equal(initialDraftingPreflight({ drafting_allowed: true }), "unchecked");
});
