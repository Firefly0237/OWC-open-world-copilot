<script setup lang="ts">
import { computed, onUnmounted, reactive, ref } from "vue";
import PageHead from "../components/PageHead.vue";
import {
  ApiError,
  currentTeamBearer,
  setTeamBearer,
  teamApiGet,
  teamApiPost,
  teamApiUpload,
} from "../api";
import {
  allowedLineageKindsForDecision,
  canCreateIntakeLineage,
  initialDraftingPreflight,
  isIntakeReadOnly,
  type DraftingPreflightStatus,
  type TeamIntakeLineageKind,
} from "../teamIntakeState";
import { notifyError } from "../toast";
import { askConfirm } from "../confirm";

type ContentRole = "CANON_CANDIDATE" | "REFERENCE" | "MIXED" | "UNKNOWN";
type DecisionKind = "CONFIRM" | "OVERRIDE" | "REJECT";
type UseKind = "ANALYSIS" | "RETRIEVAL" | "DRAFTING" | "TRAINING" | "PUBLICATION";
type LineageKind = TeamIntakeLineageKind;

interface SegmentView {
  segment_id: string;
  ordinal: number;
  text: string;
  text_hash: string;
  locator: Record<string, unknown>;
  untrusted_input: boolean;
}

interface VerdictView {
  segment_id: string;
  content_role: ContentRole;
  confidence: number;
  abstain: boolean;
  abstain_reason?: string | null;
}

interface IntakeView {
  run_id: string;
  status: string;
  processing_code?: string | null;
  artifact?: {
    artifact_id?: string;
    artifact_version_id?: string;
    content_hash?: string;
    mime_type?: string;
    byte_size?: number;
    logical_deleted_at?: string | null;
  };
  segments?: SegmentView[];
  triage_verdicts?: VerdictView[];
  rights?: {
    rights_declaration_id: string;
    artifact_version_id: string;
    rights_provenance: "OWNED" | "LICENSED" | "THIRD_PARTY" | "UNKNOWN";
    declaration_text: string;
    declared_by: string;
    declared_at: string;
  } | null;
  allowed_uses?: Record<UseKind, boolean>;
  human_decisions?: Array<{
    segment_id: string;
    decision: DecisionKind;
    content_role: ContentRole;
    rationale: string;
  }>;
  lineage?: Array<{
    lineage_anchor_id: string;
    segment_id: string;
    lineage_kind: LineageKind;
    target_ref: string;
  }>;
  drafting_allowed?: boolean;
  submitted?: boolean;
  logical_deleted?: boolean;
  segment_pagination?: {
    total: number;
    limit: number;
    offset: number;
    page_cursor?: string | null;
    previous_cursor?: string | null;
    next_cursor?: string | null;
    snapshot_etag: string;
  };
}

interface DecisionDraft {
  decision: DecisionKind;
  content_role: ContentRole;
  rationale: string;
}

interface LineageDraft {
  lineage_kind: LineageKind;
  target_ref: string;
}

const containerId = ref(localStorage.getItem("owcopilot_team_container") ?? "");
const bearer = ref(currentTeamBearer());
const rememberBearer = ref(Boolean(localStorage.getItem("owcopilot_team_bearer")));
const runIdInput = ref(localStorage.getItem("owcopilot_team_run") ?? "");
const replacementArtifactId = ref("");
const selectedFile = ref<File | null>(null);
const busy = ref(false);
const view = ref<IntakeView | null>(null);
const pageNotice = ref("");
const draftingPreflight = ref<DraftingPreflightStatus>("unchecked");
const declarationText = ref("");
const provenance = ref<"OWNED" | "LICENSED" | "THIRD_PARTY" | "UNKNOWN">("UNKNOWN");
const uses = reactive<Record<UseKind, boolean>>({
  ANALYSIS: false,
  RETRIEVAL: false,
  DRAFTING: false,
  TRAINING: false,
  PUBLICATION: false,
});
const decisions = reactive<Record<string, DecisionDraft>>({});
const lineageDrafts = reactive<Record<string, LineageDraft>>({});
const SEGMENT_PAGE_SIZE = 50;
let pageRequestSerial = 0;

const segments = computed(() => view.value?.segments ?? []);
const segmentTotal = computed(() => view.value?.segment_pagination?.total ?? segments.value.length);
const reviewedSegmentCount = computed(() => Object.keys(decisions).length);
const allSegmentsLoaded = computed(
  () => segmentTotal.value > 0 && reviewedSegmentCount.value >= segmentTotal.value,
);
const decisionsValid = computed(() =>
  Object.values(decisions).every((decision) => decision.rationale.trim().length > 0),
);
const segmentPageCount = computed(() =>
  Math.max(1, Math.ceil(segmentTotal.value / SEGMENT_PAGE_SIZE)),
);
const currentSegmentPage = computed(() =>
  Math.floor((view.value?.segment_pagination?.offset ?? 0) / SEGMENT_PAGE_SIZE) + 1,
);
const readOnly = computed(() => isIntakeReadOnly(view.value));
const canAddLineage = computed(() => canCreateIntakeLineage(view.value));
const canLoad = computed(
  () => Boolean(containerId.value.trim() && bearer.value.trim() && runIdInput.value.trim() && !busy.value),
);
const replacingDeletedArtifact = computed(
  () =>
    readOnly.value &&
    Boolean(replacementArtifactId.value.trim()) &&
    replacementArtifactId.value.trim() === view.value?.artifact?.artifact_id,
);
const verdictBySegment = computed(() => {
  const result: Record<string, VerdictView> = {};
  for (const verdict of view.value?.triage_verdicts ?? []) result[verdict.segment_id] = verdict;
  return result;
});
const canUpload = computed(
  () =>
    Boolean(containerId.value.trim() && bearer.value.trim() && selectedFile.value && !busy.value) &&
    !replacingDeletedArtifact.value,
);
const draftingPreflightText = computed(() => {
  if (draftingPreflight.value === "allowed") return "通过：当前 run 可用于 DRAFTING";
  if (draftingPreflight.value === "denied") return "拒绝：当前 run 不可用于 DRAFTING";
  if (draftingPreflight.value === "error") return "检查失败：未获得 DRAFTING 授权";
  return "尚未执行使用前授权检查";
});

function saveConnection(): void {
  localStorage.setItem("owcopilot_team_container", containerId.value.trim());
  setTeamBearer(bearer.value, rememberBearer.value);
}

function chooseFile(event: Event): void {
  const input = event.target as HTMLInputElement;
  selectedFile.value = input.files?.[0] ?? null;
}

function syncDecisionDrafts(result: IntakeView, reset = false): void {
  if (reset) for (const key of Object.keys(decisions)) delete decisions[key];
  const verdicts: Record<string, VerdictView> = {};
  for (const verdict of result.triage_verdicts ?? []) verdicts[verdict.segment_id] = verdict;
  const recorded = new Map(
    (result.human_decisions ?? []).map((decision) => [decision.segment_id, decision]),
  );
  for (const segment of result.segments ?? []) {
    const finalDecision = recorded.get(segment.segment_id);
    const suggested = verdicts[segment.segment_id];
    if (!reset && !finalDecision && decisions[segment.segment_id]) continue;
    decisions[segment.segment_id] = {
      decision: finalDecision?.decision ?? (suggested?.abstain ? "OVERRIDE" : "CONFIRM"),
      content_role: finalDecision?.content_role ?? suggested?.content_role ?? "UNKNOWN",
      rationale:
        finalDecision?.rationale ??
        (suggested?.abstain ? "人工复核弃权片段" : "人工确认模型分类"),
    };
  }
}

function syncLineageDrafts(result: IntakeView, reset = false): void {
  if (reset) for (const key of Object.keys(lineageDrafts)) delete lineageDrafts[key];
  for (const segment of result.segments ?? []) {
    if (!reset && lineageDrafts[segment.segment_id]) continue;
    const role = decisions[segment.segment_id]?.content_role;
    lineageDrafts[segment.segment_id] = {
      lineage_kind: role === "CANON_CANDIDATE" ? "SOURCE_EVIDENCE" : "INSPIRATION_LINEAGE",
      target_ref: "",
    };
  }
}

function syncRightsForm(result: IntakeView): void {
  declarationText.value = result.rights?.declaration_text ?? "";
  provenance.value = result.rights?.rights_provenance ?? "UNKNOWN";
  for (const kind of Object.keys(uses) as UseKind[]) {
    uses[kind] = result.allowed_uses?.[kind] ?? false;
  }
}

function applyLoadedView(result: IntakeView, resetDrafts = false): void {
  const changedRun = view.value?.run_id !== result.run_id;
  const shouldReset = resetDrafts || changedRun;
  view.value = result;
  runIdInput.value = result.run_id;
  localStorage.setItem("owcopilot_team_run", result.run_id);
  syncDecisionDrafts(result, shouldReset);
  syncLineageDrafts(result, shouldReset);
  syncRightsForm(result);
  draftingPreflight.value = initialDraftingPreflight(result);
}

function lineageForSegment(segmentId: string): IntakeView["lineage"] {
  return (view.value?.lineage ?? []).filter((anchor) => anchor.segment_id === segmentId);
}

function allowedLineageKinds(segmentId: string): LineageKind[] {
  return allowedLineageKindsForDecision(decisions[segmentId]);
}

function canAnchorSegment(segmentId: string): boolean {
  const draft = lineageDrafts[segmentId];
  return Boolean(
    canAddLineage.value &&
      draft &&
      allowedLineageKinds(segmentId).includes(draft.lineage_kind),
  );
}

async function loadRun(): Promise<void> {
  if (!canLoad.value) return;
  await loadSegmentPage(null, true);
}

function intakeRunPath(cursor: string | null): string {
  const params = new URLSearchParams({ limit: String(SEGMENT_PAGE_SIZE) });
  if (cursor) params.set("cursor", cursor);
  return `/team/containers/${encodeURIComponent(containerId.value.trim())}/intake-runs/${encodeURIComponent(runIdInput.value.trim())}?${params.toString()}`;
}

async function loadSegmentPage(cursor: string | null, resetDrafts = false): Promise<void> {
  if (!containerId.value.trim() || !bearer.value.trim() || !runIdInput.value.trim()) return;
  const sequence = ++pageRequestSerial;
  busy.value = true;
  pageNotice.value = "";
  saveConnection();
  try {
    const result = await teamApiGet<IntakeView>(intakeRunPath(cursor));
    if (sequence !== pageRequestSerial) return;
    applyLoadedView(result, resetDrafts);
  } catch (error) {
    if (error instanceof ApiError && error.status === 409 && cursor) {
      try {
        const refreshed = await teamApiGet<IntakeView>(intakeRunPath(null));
        if (sequence !== pageRequestSerial) return;
        applyLoadedView(refreshed, true);
        pageNotice.value = "Run 在翻页期间已更新；已丢弃旧草稿并安全回到最新第一页。";
      } catch (refreshError) {
        if (sequence === pageRequestSerial) notifyError(refreshError);
      }
    } else if (sequence === pageRequestSerial) {
      notifyError(error);
    }
  } finally {
    if (sequence === pageRequestSerial) busy.value = false;
  }
}

async function upload(): Promise<void> {
  if (!canUpload.value || !selectedFile.value) return;
  busy.value = true;
  saveConnection();
  try {
    const result = await teamApiUpload<IntakeView>(
      containerId.value.trim(),
      selectedFile.value,
      replacementArtifactId.value,
    );
    applyLoadedView(result, true);
  } catch (error) {
    notifyError(error);
  } finally {
    busy.value = false;
  }
}

async function declareRights(): Promise<void> {
  if (
    !view.value ||
    view.value.submitted ||
    readOnly.value ||
    !declarationText.value.trim() ||
    busy.value
  )
    return;
  busy.value = true;
  saveConnection();
  try {
    const path = `/team/containers/${encodeURIComponent(containerId.value.trim())}/intake-runs/${encodeURIComponent(view.value.run_id)}/rights-declarations`;
    const result = await teamApiPost<IntakeView>(path, {
      rights_provenance: provenance.value,
      declaration_text: declarationText.value.trim(),
      allowed_uses: { ...uses },
    });
    // A rights declaration changes governance metadata, not the reviewer edits already
    // made in the per-segment form.  Keep those drafts intact when replacing the view.
    applyLoadedView(result);
    pageNotice.value = "权利声明已保存；片段列表已回到最新第一页。";
  } catch (error) {
    notifyError(error);
  } finally {
    busy.value = false;
  }
}

async function submitDecisions(): Promise<void> {
  if (
    !view.value ||
    view.value.submitted ||
    readOnly.value ||
    !allSegmentsLoaded.value ||
    !decisionsValid.value ||
    busy.value
  )
    return;
  busy.value = true;
  saveConnection();
  try {
    const path = `/team/containers/${encodeURIComponent(containerId.value.trim())}/intake-runs/${encodeURIComponent(view.value.run_id)}:submit`;
    const result = await teamApiPost<IntakeView>(path, {
      decisions: Object.entries(decisions).map(([segmentId, decision]) => ({
        segment_id: segmentId,
        ...decision,
      })),
    });
    applyLoadedView(result, true);
  } catch (error) {
    notifyError(error);
  } finally {
    busy.value = false;
  }
}

async function createLineage(segmentId: string): Promise<void> {
  const draft = lineageDrafts[segmentId];
  if (!view.value || !canAnchorSegment(segmentId) || !draft?.target_ref.trim() || busy.value)
    return;
  busy.value = true;
  saveConnection();
  try {
    const path = `/team/containers/${encodeURIComponent(containerId.value.trim())}/intake-runs/${encodeURIComponent(view.value.run_id)}/lineage-anchors`;
    const result = await teamApiPost<IntakeView>(path, {
      segment_id: segmentId,
      lineage_kind: draft.lineage_kind,
      target_ref: draft.target_ref.trim(),
    });
    applyLoadedView(result);
    pageNotice.value = "Lineage 已保存；片段列表已回到最新第一页。";
    draft.target_ref = "";
  } catch (error) {
    notifyError(error);
  } finally {
    busy.value = false;
  }
}

async function checkDraftingAuthorization(): Promise<void> {
  if (!view.value || readOnly.value || busy.value) {
    if (readOnly.value) draftingPreflight.value = "denied";
    return;
  }
  busy.value = true;
  saveConnection();
  try {
    const path = `/team/containers/${encodeURIComponent(containerId.value.trim())}/intake-runs/${encodeURIComponent(view.value.run_id)}:authorize-drafting`;
    const result = await teamApiPost<{ allowed: boolean }>(path, {});
    draftingPreflight.value = result.allowed ? "allowed" : "denied";
  } catch (error) {
    if (error instanceof Error && error.message.startsWith("403 ")) {
      draftingPreflight.value = "denied";
    } else {
      draftingPreflight.value = "error";
      notifyError(error);
    }
  } finally {
    busy.value = false;
  }
}

async function logicalDelete(): Promise<void> {
  if (!view.value || readOnly.value || busy.value) return;
  const confirmed = await askConfirm(
    "逻辑删除是单向操作。原始字节与定位信息会保留供审计，但该 artifact 将永久只读且不能授权 DRAFTING。确定继续吗？",
    { title: "逻辑删除来源", confirmLabel: "确认逻辑删除" },
  );
  if (!confirmed) return;
  busy.value = true;
  saveConnection();
  try {
    const path = `/team/containers/${encodeURIComponent(containerId.value.trim())}/intake-runs/${encodeURIComponent(view.value.run_id)}:logical-delete`;
    const result = await teamApiPost<IntakeView>(path, {});
    applyLoadedView(result, true);
    draftingPreflight.value = "denied";
  } catch (error) {
    notifyError(error);
  } finally {
    busy.value = false;
  }
}

onUnmounted(() => {
  pageRequestSerial += 1;
});
</script>

<template>
  <section>
    <PageHead
      overline="SOURCE INTAKE"
      title="来源摄取"
      purpose="上传 DOCX / PDF / XLSX，逐段确认内容角色、权属与允许用途。"
    />

    <div class="pane form">
      <div class="grid two">
        <label class="field">
          <span class="label">团队容器 UUID</span>
          <input v-model="containerId" autocomplete="off" placeholder="xxxxxxxx-xxxx-…" />
        </label>
        <label class="field">
          <span class="label">团队 Bearer Token</span>
          <input v-model="bearer" type="password" autocomplete="current-password" placeholder="仅用于团队 API" />
          <span class="token-memory"><input v-model="rememberBearer" type="checkbox" /> 在这台设备长期记住</span>
        </label>
      </div>
      <div class="grid run-loader">
        <label class="field">
          <span class="label">已有 Run UUID</span>
          <input v-model="runIdInput" autocomplete="off" placeholder="输入 run_id 恢复审阅现场" />
        </label>
        <button class="secondary" :disabled="!canLoad" @click="loadRun">恢复 / 查看 Run</button>
      </div>
      <label class="field">
        <span class="label">DOCX / PDF / XLSX 文件</span>
        <input type="file" accept=".docx,.pdf,.xlsx" @change="chooseFile" />
      </label>
      <label class="field">
        <span class="label">替换既有 Artifact UUID（可选）</span>
        <input
          v-model="replacementArtifactId"
          autocomplete="off"
          placeholder="留空创建新 artifact；填写后创建不可变的新版本"
        />
      </label>
      <p class="muted small">
        服务端按真实字节计算 SHA-256，并核对后缀、MIME、OOXML 内部类型或 PDF 结构；文件正文始终按不可信输入处理。
      </p>
      <p v-if="replacementArtifactId.trim()" class="muted small">
        Replacement：新文件会沿用该 artifact 身份，但生成新的不可变 artifact_version 与 run。
      </p>
      <p v-if="replacingDeletedArtifact" class="danger small">
        当前 artifact 已逻辑删除，不能再创建 replacement；请清空 Artifact UUID 后创建新来源。
      </p>
      <button class="primary" :disabled="!canUpload" @click="upload">
        {{ busy ? "处理中…" : "上传并分段" }}
      </button>
    </div>

    <template v-if="view">
      <p v-if="pageNotice" class="pane page-notice" role="status">{{ pageNotice }}</p>
      <div v-if="readOnly" class="pane readonly-banner">
        <b>此 Artifact 已逻辑删除，当前页面只读。</b>
        <span>原始字节、哈希、定位与 lineage 仍保留供审计；DRAFTING 永久按未授权处理。</span>
      </div>
      <div class="pane summary">
        <div><b>Run</b><code>{{ view.run_id }}</code></div>
        <div><b>状态</b><span>{{ view.status }}</span></div>
        <div v-if="view.processing_code">
          <b>处理代码</b><code>{{ view.processing_code }}</code>
        </div>
        <div><b>Artifact</b><code>{{ view.artifact?.artifact_id }}</code></div>
        <div><b>Version</b><code>{{ view.artifact?.artifact_version_id }}</code></div>
        <div><b>SHA-256</b><code>{{ view.artifact?.content_hash }}</code></div>
        <div><b>字节数</b><span>{{ view.artifact?.byte_size }}</span></div>
      </div>

      <div class="pane">
        <h3>逐段人工确认</h3>
        <p class="muted small">
          当前第 {{ currentSegmentPage }} / {{ segmentPageCount }} 页；已载入并保留
          {{ reviewedSegmentCount }} / {{ segmentTotal }} 个片段的人工草稿。
        </p>
        <article v-for="segment in segments" :key="segment.segment_id" class="segment">
          <div class="segment-head">
            <b>#{{ segment.ordinal + 1 }}</b>
            <code>{{ JSON.stringify(segment.locator) }}</code>
            <span class="warn-tag">不可信输入</span>
          </div>
          <pre>{{ segment.text }}</pre>
          <p class="muted small">
            模型建议：{{ verdictBySegment[segment.segment_id]?.content_role ?? "UNKNOWN" }} ·
            置信度 {{ (verdictBySegment[segment.segment_id]?.confidence ?? 0).toFixed(2) }}
            <b v-if="verdictBySegment[segment.segment_id]?.abstain"> · 已弃权</b>
          </p>
          <div class="grid three">
            <select
              v-model="decisions[segment.segment_id].decision"
              :aria-label="`片段 ${segment.ordinal + 1} 的人工裁决`"
              :disabled="busy || view.submitted || readOnly"
            >
              <option value="CONFIRM">确认建议</option>
              <option value="OVERRIDE">人工改判</option>
              <option value="REJECT">拒绝片段</option>
            </select>
            <select
              v-model="decisions[segment.segment_id].content_role"
              :aria-label="`片段 ${segment.ordinal + 1} 的内容角色`"
              :disabled="busy || view.submitted || readOnly"
            >
              <option value="CANON_CANDIDATE">正典候选</option>
              <option value="REFERENCE">参考素材</option>
              <option value="MIXED">混合内容</option>
              <option value="UNKNOWN">未知</option>
            </select>
            <input
              v-model="decisions[segment.segment_id].rationale"
              :aria-label="`片段 ${segment.ordinal + 1} 的复核理由`"
              :disabled="busy || view.submitted || readOnly"
              placeholder="人工复核理由"
            />
          </div>
          <div v-if="view.submitted" class="lineage-box">
            <b>Lineage anchors</b>
            <ul v-if="lineageForSegment(segment.segment_id)?.length" class="anchor-list">
              <li
                v-for="anchor in lineageForSegment(segment.segment_id)"
                :key="anchor.lineage_anchor_id"
              >
                <code>{{ anchor.lineage_kind }}</code> → {{ anchor.target_ref }}
              </li>
            </ul>
            <p v-else class="muted small">尚未为此片段建立下游 lineage。</p>
            <div class="grid lineage-editor">
              <select
                v-model="lineageDrafts[segment.segment_id].lineage_kind"
                :aria-label="`片段 ${segment.ordinal + 1} 的 lineage 类型`"
                :disabled="busy || !canAnchorSegment(segment.segment_id)"
              >
                <option
                  value="SOURCE_EVIDENCE"
                  :disabled="!allowedLineageKinds(segment.segment_id).includes('SOURCE_EVIDENCE')"
                >
                  SOURCE_EVIDENCE（事实证据）
                </option>
                <option
                  value="INSPIRATION_LINEAGE"
                  :disabled="
                    !allowedLineageKinds(segment.segment_id).includes('INSPIRATION_LINEAGE')
                  "
                >
                  INSPIRATION（灵感来源）
                </option>
              </select>
              <input
                v-model="lineageDrafts[segment.segment_id].target_ref"
                :aria-label="`片段 ${segment.ordinal + 1} 的 lineage 目标`"
                :disabled="busy || !canAnchorSegment(segment.segment_id)"
                placeholder="target_ref，例如 canon:character-42 或 draft:quest-7"
              />
              <button
                class="secondary"
                :disabled="
                  busy ||
                  !canAnchorSegment(segment.segment_id) ||
                  !lineageDrafts[segment.segment_id].target_ref.trim()
                "
                @click="createLineage(segment.segment_id)"
              >
                建立 Anchor
              </button>
            </div>
            <p
              v-if="!allowedLineageKinds(segment.segment_id).length"
              class="muted small"
            >
              REJECT 或 UNKNOWN 的最终人工裁决不能建立 lineage。
            </p>
            <p v-if="readOnly" class="muted small">逻辑删除后既有 anchor 可查看，但不可新增。</p>
          </div>
        </article>
        <nav
          v-if="view.segment_pagination && segmentPageCount > 1"
          class="segment-pagination"
          aria-label="摄取片段分页"
        >
          <button
            class="secondary"
            :disabled="busy || !view.segment_pagination.previous_cursor"
            @click="loadSegmentPage(view.segment_pagination.previous_cursor ?? null)"
          >上一页</button>
          <span>服务端每页最多 {{ SEGMENT_PAGE_SIZE }} 条</span>
          <button
            class="secondary"
            :disabled="busy || !view.segment_pagination.next_cursor"
            @click="loadSegmentPage(view.segment_pagination.next_cursor ?? null)"
          >下一页</button>
        </nav>
      </div>

      <div class="pane form">
        <h3>人工权属与允许用途</h3>
        <p class="muted small">AI 不得声明法律权属。所有用途默认关闭，尤其不会默认授权写作。</p>
        <label class="field">
          <span class="label">权属来源</span>
          <select v-model="provenance" :disabled="busy || view.submitted || readOnly">
            <option value="OWNED">自有</option>
            <option value="LICENSED">已获许可</option>
            <option value="THIRD_PARTY">第三方</option>
            <option value="UNKNOWN">未知</option>
          </select>
        </label>
        <label class="field">
          <span class="label">人工声明</span>
          <textarea
            v-model="declarationText"
            rows="3"
            :disabled="busy || view.submitted || readOnly"
            placeholder="说明权属依据与限制"
          ></textarea>
        </label>
        <div class="uses">
          <label v-for="(_, kind) in uses" :key="kind" class="check">
            <input v-model="uses[kind]" type="checkbox" :disabled="busy || view.submitted || readOnly" />
            <span>{{ kind }}</span>
          </label>
        </div>
        <button
          class="secondary"
          :disabled="busy || view.submitted || readOnly || !declarationText.trim()"
          @click="declareRights"
        >
          保存人工声明
        </button>
        <p class="drafting" :class="{ allowed: view.drafting_allowed }">
          DRAFTING：{{ view.drafting_allowed ? "已由人工明确授权" : "未授权（默认拒绝）" }}
        </p>
        <button
          class="primary"
          :disabled="busy || view.submitted || readOnly || !view.rights || !allSegmentsLoaded || !decisionsValid"
          @click="submitDecisions"
        >
          {{ view.submitted ? "已提交人工裁决" : "提交全部人工裁决" }}
        </button>
        <p v-if="!view.submitted && !allSegmentsLoaded" class="muted small">
          请依次打开所有片段页后再整体提交；尚有 {{ Math.max(0, segmentTotal - reviewedSegmentCount) }} 个片段未载入。
        </p>
        <p v-else-if="!view.submitted && !decisionsValid" class="danger small">
          每个片段都需要填写人工复核理由。
        </p>
      </div>

      <div class="pane form lifecycle">
        <h3>DRAFTING 授权预检与 Artifact 生命周期</h3>
        <p class="muted small">
          声明状态：{{ view.drafting_allowed ? "DRAFTING 已由人工勾选" : "DRAFTING 未授权" }}。
          真正进入写作前仍需执行一次服务端 preflight。
        </p>
        <p
          class="preflight"
          :class="{
            allowed: draftingPreflight === 'allowed',
            denied: draftingPreflight === 'denied' || draftingPreflight === 'error',
          }"
        >
          {{ draftingPreflightText }}
        </p>
        <div class="actions">
          <button
            class="secondary"
            :disabled="busy || readOnly"
            @click="checkDraftingAuthorization"
          >
            检查 DRAFTING 授权
          </button>
          <button class="danger-button" :disabled="busy || readOnly" @click="logicalDelete">
            {{ readOnly ? "Artifact 已逻辑删除" : "逻辑删除 Artifact" }}
          </button>
        </div>
        <p class="muted small">
          逻辑删除不会销毁审计所需字节、哈希、精确定位和 lineage，但会使该 artifact 永久只读并拒绝所有 DRAFTING preflight。
        </p>
      </div>
    </template>
  </section>
</template>

<style scoped>
.form { padding: 1rem 1.2rem; display: flex; flex-direction: column; gap: .8rem; }
.grid { display: grid; gap: .7rem; }
.two { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.three { grid-template-columns: .8fr 1fr 2fr; }
.run-loader { grid-template-columns: minmax(0, 1fr) auto; align-items: end; }
.field { display: flex; flex-direction: column; gap: .35rem; }
.label { font-size: .82rem; color: var(--text-muted); }
.token-memory { display: flex; align-items: center; gap: .35rem; color: var(--ow-muted); font-size: .74rem; }
.token-memory input { width: 1rem; height: 1rem; accent-color: var(--ow-gold-bright); }
.summary { display: grid; grid-template-columns: 1fr 1fr; gap: .6rem 1rem; }
.summary div { display: flex; gap: .6rem; min-width: 0; }
code { overflow-wrap: anywhere; color: var(--gold, #c9a66b); }
.segment { padding: .9rem 0; border-top: 1px solid var(--border); }
.segment-head { display: flex; flex-wrap: wrap; align-items: center; gap: .6rem; }
.warn-tag { color: #d8a45a; font-size: .75rem; }
.readonly-banner { display: flex; flex-direction: column; gap: .35rem; border-color: #a75c5c; color: #e5b0a8; }
.lineage-box { margin-top: .8rem; padding: .8rem; border: 1px solid var(--border); border-radius: .4rem; }
.lineage-editor { grid-template-columns: 1fr 2fr auto; align-items: center; }
.anchor-list { margin: .5rem 0; padding-left: 1.2rem; }
.page-notice { padding: .7rem .9rem; color: var(--ow-gold-bright); }
.segment-pagination {
  display: flex; align-items: center; justify-content: center; gap: .7rem; margin-top: .8rem;
}
.segment-pagination span { color: var(--ow-muted); font-size: .76rem; }
pre { white-space: pre-wrap; font: inherit; line-height: 1.6; }
.uses { display: flex; flex-wrap: wrap; gap: .5rem 1rem; }
.drafting { color: #d8a45a; }
.drafting.allowed { color: #75bd8a; }
.preflight { margin: 0; color: #d8a45a; }
.preflight.allowed { color: #75bd8a; }
.preflight.denied, .danger { color: #e19287; }
.actions { display: flex; flex-wrap: wrap; gap: .7rem; }
.danger-button { border-color: #a75c5c; color: #e5b0a8; }
.small { font-size: .82rem; }
@media (max-width: 780px) {
  .two, .three, .summary, .run-loader, .lineage-editor { grid-template-columns: 1fr; }
  .segment-pagination { justify-content: space-between; }
  .segment-pagination button { min-height: 44px; }
}
</style>
