export interface TeamIntakeLifecycleView {
  submitted?: boolean;
  logical_deleted?: boolean;
  drafting_allowed?: boolean;
  artifact?: {
    artifact_id?: string;
    logical_deleted_at?: string | null;
  };
}

export type DraftingPreflightStatus = "unchecked" | "allowed" | "denied" | "error";
export type TeamIntakeLineageKind = "SOURCE_EVIDENCE" | "INSPIRATION_LINEAGE";

export interface TeamIntakeFinalDecision {
  decision: "CONFIRM" | "OVERRIDE" | "REJECT";
  content_role: "CANON_CANDIDATE" | "REFERENCE" | "MIXED" | "UNKNOWN";
}

export function isIntakeReadOnly(view: TeamIntakeLifecycleView | null): boolean {
  return Boolean(view?.logical_deleted || view?.artifact?.logical_deleted_at);
}

export function canCreateIntakeLineage(view: TeamIntakeLifecycleView | null): boolean {
  return Boolean(view?.submitted && !isIntakeReadOnly(view));
}

export function allowedLineageKindsForDecision(
  decision: TeamIntakeFinalDecision | undefined,
): TeamIntakeLineageKind[] {
  if (!decision || decision.decision === "REJECT" || decision.content_role === "UNKNOWN") return [];
  return decision.content_role === "CANON_CANDIDATE"
    ? ["SOURCE_EVIDENCE"]
    : ["INSPIRATION_LINEAGE"];
}

export function initialDraftingPreflight(
  view: TeamIntakeLifecycleView | null,
): DraftingPreflightStatus {
  return isIntakeReadOnly(view) ? "denied" : "unchecked";
}

export function buildTeamIntakeUploadPath(
  containerId: string,
  filename: string,
  artifactId = "",
): string {
  const query = new URLSearchParams({ filename });
  const replacement = artifactId.trim();
  if (replacement) query.set("artifact_id", replacement);
  return `/team/containers/${encodeURIComponent(containerId)}/intake-runs?${query.toString()}`;
}
