import { apiDownload, apiGet, apiPost } from "./api";

export interface SkillCardSlot {
  readonly name: string; readonly kind: string; readonly binding_key: string;
  readonly source_value?: string;
}
export interface SkillCardStep {
  title: string; instruction: string; operation?: string;
  readonly slots?: readonly SkillCardSlot[];
}
export interface SkillCardDefinition {
  name: string; purpose: string; steps: SkillCardStep[];
  applies_to: string[]; does_not_apply_to: string[];
}
export interface SkillCardVersion {
  id: string; version: number; version_hash: string; status: string;
  changes: string[]; created_at: string;
  definition?: SkillCardDefinition;
}
export interface SkillCard {
  id: string; card_hash: string; name: string; purpose: string;
  steps: SkillCardStep[]; applies_to: string[]; does_not_apply_to: string[];
  source_summary: string; versions: SkillCardVersion[];
  active_version_id: string | null; current_version_id: string; enabled: boolean;
  maintenance_notice?: string;
}
export interface SkillCardSelection { card_id: string; version_id: string; version_hash: string }
export type SkillUsageStatus = "bound" | "dispatched" | "response_received" | "unknown";
export interface RunSkillUsage {
  project: string; task_id: string; run_id: string; revision: number;
  skills: {
    card_id: string; version_id: string; version_hash: string; name: string; version: number | null;
    selection: "explicit_task" | "adopted_default";
    steps: { id: string; title: string; operation: string; node_ids: string[]; status: SkillUsageStatus;
      nodes?: { id: string; status: SkillUsageStatus }[] }[];
  }[];
  coverage: "complete" | "partial" | "unavailable";
  semantic_compliance: "unknown";
  source?: { task_origin: string; actor_origin: string; collection_condition: string };
  measurements?: { dispatch_count: number; total_tokens: number | null; usage_complete: boolean; cost_status: string };
}
export interface SkillPackageFile { path: string; content: string }
export interface SkillPackageManifest { files: SkillPackageFile[] }
export interface SkillSummaryResult {
  status?: string; card_id?: string; version_id?: string; reason?: string; error?: string;
  source_run_id?: string; new_dispatch?: boolean; result?: SkillSummaryResult | null;
  maintenance_recording_error?: string | null;
}
export interface SkillCardJob {
  job_id: string; status: string; error?: string | null; source_run_id?: string;
  result?: SkillSummaryResult | null;
}

const encoded = encodeURIComponent;
export function skillCardsClient(project: string) {
  const base = `/projects/${encoded(project)}/creative/v2/skill-cards`;
  return {
    list: () => apiGet<{ cards: SkillCard[] }>(base),
    fromRun: (runId: string, operationId: string, name?: string) =>
      apiPost<SkillCardJob>(`${base}/from-run`, { run_id: runId, operation_id: operationId, ...(name ? { name } : {}) }),
    update: (card: SkillCard, versionId: string, runId: string, feedback: string, operationId: string) =>
      apiPost<SkillCardJob>(`${base}/${encoded(card.id)}/update`, {
        expected_card_hash: card.card_hash, base_version_id: versionId,
        run_id: runId, feedback, operation_id: operationId,
      }),
    adopt: (card: SkillCard, versionId: string, makeDefault: boolean) =>
      apiPost<SkillCard>(`${base}/${encoded(card.id)}/adopt`, {
        expected_card_hash: card.card_hash, version_id: versionId, make_default: makeDefault,
      }),
    enable: (card: SkillCard, enabled: boolean) => apiPost<SkillCard>(`${base}/${encoded(card.id)}/enable`, {
      expected_card_hash: card.card_hash, enabled,
    }),
    rollback: (card: SkillCard, versionId: string) => apiPost<SkillCard>(`${base}/${encoded(card.id)}/rollback`, {
      expected_card_hash: card.card_hash, version_id: versionId,
    }),
    exportPackage: (cardId: string, versionId: string) => apiDownload(
      `${base}/${encoded(cardId)}/versions/${encoded(versionId)}/package`, "skill.zip",
    ),
    exportManifest: (cardId: string, versionId: string) => apiGet<SkillPackageManifest>(
      `${base}/${encoded(cardId)}/versions/${encoded(versionId)}/package-manifest`,
    ),
    importPackage: (manifest: SkillPackageManifest) => apiPost<SkillCard>(`${base}/import`, manifest),
    job: (jobId: string) => apiGet<SkillCardJob>(`/jobs/${encoded(jobId)}`),
  };
}
