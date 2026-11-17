<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { humanizeError } from "../api";
import StoryPlaytest from "./StoryPlaytest.vue";
import { storyCreatorClient, type ApplicationContinuation, type PreviewCompatibility, type NarrativeClaim, type NarrativePreview, type NarrativeProject, type NarrativeSource, type ProjectOutline, type SourceScope } from "../storyCreatorApi";
import type { StudioPreview } from "../studioApi";

const props = defineProps<{ workspace: string; selectedProjectId?: string; selectedSceneId?: string; artifactSource?: { artifact_id: string; artifact_hash: string; kind: string } | null; canBind: boolean; locked: boolean }>();
const emit = defineEmits<{ select: [projectId: string, sceneId: string, ready: boolean, path?: { preview_id: string; transition_id?: string }]; changed: [] }>();
let client = storyCreatorClient(props.workspace);
let epoch = 0;
let alive = true;
const busy = ref(false);
const error = ref("");
const notice = ref("");
const projects = ref<NarrativeProject[]>([]);
const projectId = ref("");
const current = ref<NarrativeProject | null>(null);
const projectName = ref("");
const projectDescription = ref("");
const outline = ref<ProjectOutline | null>(null);
const dirty = ref(false);
const sceneId = ref("");
const chapterId = ref("");
const sceneSearch = ref("");
const scenePage = ref(0);
const sources = ref<NarrativeSource[]>([]);
const claims = ref<NarrativeClaim[]>([]);
const sourceName = ref("");
const sourceText = ref("");
const sourceFormat = ref("text");
const sourceStatus = ref("draft");
const sourceScope = ref<"project" | "chapter" | "scene">("scene");
const allowFacts = ref(false);
const attachSource = ref(true);
const sourceLocator = ref("");
const factSourceId = ref("");
const factSubject = ref("");
const factPredicate = ref("");
const factValue = ref("");
const factQuote = ref("");
const quoteOccurrence = ref(0);
const rejectIds = ref<Record<string, string[]>>({});
const preview = ref<NarrativePreview | null>(null);
const preservedPreviews = ref<NarrativePreview[]>([]);
const previewIssue = ref("");
const compatibility = ref<PreviewCompatibility | null>(null);
const compatibilityScope = ref<{ project_id: string; project_hash: string; preview_id: string; preview_hash: string } | null>(null);
const fixedHistorical = computed(() => preview.value?.version_mode === "fixed_historical");
const pendingStep = ref<{ preview_id: string; action_id: string; expected_seq: number; choice_id?: string; transition_id?: string } | null>(null);
const pendingPreviewRead = ref<{ project_id: string; preview_id: string; notice: string } | null>(null);
const generationPreviewId = ref("");
const generationTransitionId = ref("");
const generationSceneId = ref("");
const generationSequence = ref<number | null>(null);
const archiveName = ref("");
const archives = ref<{ id: string; name: string; created_at: string; project_hash: string }[]>([]);

const selectedScene = computed(() => outline.value?.scenes.find((item) => item.id === sceneId.value));
const selectedChapter = computed(() => outline.value?.chapters.find((item) => item.id === chapterId.value));
const sceneResults = computed(() => (outline.value?.scenes ?? []).filter((item) => (!chapterId.value || item.chapter_id === chapterId.value) && (!sceneSearch.value || `${item.title} ${item.brief}`.includes(sceneSearch.value))));
const visibleScenes = computed(() => sceneResults.value.slice(scenePage.value * 20, scenePage.value * 20 + 20));
const outgoing = computed(() => outline.value?.branches.filter((item) => item.source_scene === sceneId.value) ?? []);
const factSource = computed(() => sources.value.find((item) => item.id === factSourceId.value));
const occurrences = computed(() => {
  const text = factSource.value?.source.text ?? ""; const quote = factQuote.value;
  if (!quote) return [];
  const matches: { start: number; end: number; context: string }[] = [];
  let cursor = 0;
  while (matches.length < 100) {
    const position = text.indexOf(quote, cursor);
    if (position < 0) break;
    const start = Array.from(text.slice(0, position)).length;
    matches.push({ start, end: start + Array.from(quote).length, context: text.slice(Math.max(0, position - 20), position + quote.length + 20) });
    cursor = position + Math.max(1, quote.length);
  }
  return matches;
});
const previewNeedsRebase = computed(() => Boolean(!fixedHistorical.value && preview.value && current.value && (preview.value.project_hash !== current.value.project_hash || preview.value.snapshot_status === "historical_requires_rebase")));
const previewBinding = computed(() => fixedHistorical.value || previewNeedsRebase.value ? undefined : current.value?.scene_bindings[preview.value?.scene_id ?? ""]);
const compatibilityCurrent = computed(() => Boolean(compatibility.value && compatibilityScope.value
  && compatibilityScope.value.project_id === current.value?.id && compatibilityScope.value.project_hash === current.value?.project_hash
  && compatibilityScope.value.preview_id === preview.value?.preview_id && compatibilityScope.value.preview_hash === preview.value?.preview_hash));
const legacyPlayerRecord = computed(() => {
  if (!preview.value || previewNeedsRebase.value || preview.value.view.presentation === "player") return false;
  const payload = previewBinding.value?.payload;
  if (!payload?.quest) return false;
  return "mechanic_ir_v2" in (payload.quest.metadata ?? {})
    || "player_presentation_v1" in (payload.tree?.metadata ?? {})
    || "mechanic_ir_v2" in (payload.tree?.metadata ?? {});
});
const previewCanContinue = computed(() => !dirty.value && !previewNeedsRebase.value && !pendingStep.value && !pendingPreviewRead.value);
const playable = computed<StudioPreview | null>(() => preview.value ? { preview_id: preview.value.preview_id, view: { ...preview.value.view, action_seq: preview.value.action_seq, state: { ...preview.value.view.state, ...preview.value.state } } } : null);

function id(prefix: string): string { return `${prefix}_${crypto.randomUUID().replaceAll("-", "")}`; }
function sceneName(key: string | null): string { return key ? outline.value?.scenes.find((item) => item.id === key)?.title || "未命名场景" : "故事结局"; }
function valueText(value: unknown): string { return value == null ? "未设定" : typeof value === "object" ? "结构化值（调试可查）" : String(value); }
function copyOutline(value: ProjectOutline): ProjectOutline { return JSON.parse(JSON.stringify(value)) as ProjectOutline; }
function emptyOutline(): ProjectOutline { return { schema_version: 1, chapters: [], scenes: [], branches: [], variables: [], entry_scene_id: "" }; }
function publishSelection(): void { emit("select", current.value?.id ?? "", sceneId.value, !dirty.value && (!sceneId.value || Boolean(current.value?.outline?.scenes.some((scene) => scene.id === sceneId.value))), generationPreviewId.value ? { preview_id: generationPreviewId.value, ...(generationTransitionId.value ? { transition_id: generationTransitionId.value } : {}) } : undefined); }
function clearGenerationPath(): void { generationPreviewId.value = ""; generationTransitionId.value = ""; generationSceneId.value = ""; generationSequence.value = null; }
function storageKey(kind: string, target = current.value?.id ?? ""): string { return `owcopilot:narrative:${kind}:${encodeURIComponent(props.workspace)}:${target}`; }
function storedValue(key: string): string { try { return localStorage.getItem(key) ?? ""; } catch { return ""; } }
function storeValue(key: string, value: string): void { try { if (value) localStorage.setItem(key, value); else localStorage.removeItem(key); } catch { /* The visible session remains usable without browser storage. */ } }
function adoptPreview(result: NarrativePreview): void {
  if (preview.value && preview.value.preview_id !== result.preview_id) {
    preservedPreviews.value = [preview.value, ...preservedPreviews.value.filter((item) => item.preview_id !== preview.value!.preview_id && item.preview_id !== result.preview_id)].slice(0, 8);
  }
  preview.value = result; previewIssue.value = ""; compatibility.value = null; compatibilityScope.value = null;
  if (pendingPreviewRead.value?.preview_id === result.preview_id && pendingPreviewRead.value.project_id === result.project_id) pendingPreviewRead.value = null;
  if (result.version_mode === "fixed_historical") clearGenerationPath();
  if (pendingStep.value && (pendingStep.value.preview_id !== result.preview_id || result.action_seq > pendingStep.value.expected_seq)) pendingStep.value = null;
  storeValue(storageKey("active-preview", result.project_id), result.preview_id);
}
function retainPreviewAfterChange(): void {
  compatibility.value = null; compatibilityScope.value = null;
  clearGenerationPath(); publishSelection();
  if (preview.value && !fixedHistorical.value) previewIssue.value = "作品已更新，原路线与状态已保留。可检查后在新版继续，或沿旧版另开试玩。";
}
function previewSceneName(key: string): string { return fixedHistorical.value || previewNeedsRebase.value ? "历史路径中的场景" : sceneName(key); }
function usePlayedPath(transition?: { id: string; label?: string; target_scene?: string | null }): void {
  if (!preview.value || !previewCanContinue.value || fixedHistorical.value) return;
  if (transition && (preview.value.view.status !== "waiting_transition" || !transition.target_scene || !preview.value.view.choices.some((choice) => choice.id === transition.id))) return;
  const target = transition?.target_scene ?? preview.value.scene_id;
  generationPreviewId.value = preview.value.preview_id; generationTransitionId.value = transition?.id ?? ""; generationSceneId.value = target;
  generationSequence.value = preview.value.action_seq;
  sceneId.value = target; chapterId.value = outline.value?.scenes.find((scene) => scene.id === target)?.chapter_id ?? "";
  publishSelection(); notice.value = transition ? `将沿“${transition.label ?? '当前路线'}”生成下一场景，试玩位置保持不变。` : "已选当前路线，用于此场景下次生成。";
}
async function act(action: () => Promise<void>): Promise<void> {
  if (busy.value || props.locked) return;
  const stamp = epoch; busy.value = true; error.value = ""; notice.value = "";
  try { await action(); } catch (failure) { if (alive && stamp === epoch) error.value = humanizeError(failure); }
  finally { if (alive && stamp === epoch) busy.value = false; }
}
async function refreshProjects(): Promise<void> {
  const stamp = epoch; const result = await client.projects();
  if (alive && stamp === epoch) projects.value = result.projects;
}
async function loadProject(restorePreviewId?: string | null): Promise<void> {
  if (!projectId.value) { current.value = null; outline.value = null; sources.value = []; claims.value = []; sceneId.value = ""; preview.value = null; preservedPreviews.value = []; pendingStep.value = null; pendingPreviewRead.value = null; compatibility.value = null; compatibilityScope.value = null; clearGenerationPath(); publishSelection(); return; }
  const stamp = epoch; const target = projectId.value;
  const priorPreviewId = restorePreviewId !== undefined ? restorePreviewId : preview.value?.project_id === target ? preview.value.preview_id : storedValue(storageKey("active-preview", target));
  if (current.value?.id === target && restorePreviewId !== undefined && preview.value && preview.value.preview_id !== restorePreviewId) {
    preservedPreviews.value = [preview.value, ...preservedPreviews.value.filter((item) => item.preview_id !== preview.value!.preview_id)].slice(0, 8);
  }
  if (current.value?.id !== target || restorePreviewId !== undefined) { preview.value = null; pendingStep.value = null; previewIssue.value = ""; compatibility.value = null; compatibilityScope.value = null; clearGenerationPath(); }
  if (current.value?.id !== target) { preservedPreviews.value = []; pendingPreviewRead.value = null; }
  const [detail, saved] = await Promise.all([client.project(target), client.archives(target)]);
  if (!alive || stamp !== epoch || target !== projectId.value) return;
  current.value = detail.project; sources.value = detail.sources; claims.value = detail.claims; archives.value = saved.archives;
  rejectIds.value = Object.fromEntries(detail.claims.map((claim) => [claim.id, []]));
  outline.value = detail.project.outline ? copyOutline(detail.project.outline) : emptyOutline(); dirty.value = false;
  if (props.selectedSceneId && outline.value.scenes.some((item) => item.id === props.selectedSceneId)) sceneId.value = props.selectedSceneId;
  else if (!outline.value.scenes.some((item) => item.id === sceneId.value)) sceneId.value = outline.value.entry_scene_id || outline.value.scenes[0]?.id || "";
  chapterId.value = outline.value.scenes.find((item) => item.id === sceneId.value)?.chapter_id ?? outline.value.chapters[0]?.id ?? "";
  factSourceId.value = detail.sources[0]?.id ?? "";
  if (priorPreviewId) {
    try {
      const result = await client.projectPreviewState(target, priorPreviewId);
      if (!alive || stamp !== epoch || target !== projectId.value) return;
      adoptPreview(result);
    } catch (failure) {
      if (alive && stamp === epoch && target === projectId.value) previewIssue.value = `先前路线读取未成功：${humanizeError(failure)}。已有会话没有被删除，可再次读取或从头新开。`;
    }
  }
  publishSelection();
}
async function createProject(): Promise<void> {
  await act(async () => {
    if (!projectName.value.trim()) throw new Error("先给这部作品一个名字。");
    const stamp = epoch;
    const result = await client.createProject(projectName.value.trim(), projectDescription.value.trim());
    if (!alive || stamp !== epoch) return;
    projectId.value = result.project.id; projectName.value = ""; projectDescription.value = "";
    await refreshProjects(); await loadProject(); emit("changed");
  });
}
function addChapter(): void {
  outline.value ??= emptyOutline(); const key = id("chapter");
  outline.value.chapters.push({ id: key, title: "", summary: "" }); chapterId.value = key; dirty.value = true;
}
function addScene(): void {
  if (!outline.value || !chapterId.value) { error.value = "先添加并选中一个章节。"; return; }
  const key = id("scene");
  outline.value.scenes.push({ id: key, chapter_id: chapterId.value, title: "", brief: "", order: outline.value.scenes.length, dependencies: [], sources: [], precondition: "", postcondition: "", forbidden: [], merge_policy: "preserve_path_state", merge_effects: [] });
  sceneId.value = key; outline.value.entry_scene_id ||= key; dirty.value = true; publishSelection();
}
function addBranch(): void {
  if (!outline.value || !sceneId.value) return;
  outline.value.branches.push({ id: id("branch"), source_scene: sceneId.value, target_scene: null, label: "", condition: "", effects: [], ending_text: "" }); dirty.value = true;
}
function addEffect(branch: ProjectOutline["branches"][number]): void {
  const variable = outline.value?.variables[0];
  if (!variable) { error.value = "先在下方声明需要跨场景记住的状态，再为分支添加后果。"; return; }
  branch.effects.push({ var: variable.id, op: "set", value: variable.default }); dirty.value = true;
}
function effectVariableChanged(effect: { var: string; op: string; value: unknown }): void {
  effect.value = outline.value?.variables.find((variable) => variable.id === effect.var)?.default ?? false; effect.op = "set"; dirty.value = true;
}
function variableFor(key: string): ProjectOutline["variables"][number] | undefined { return outline.value?.variables.find((variable) => variable.id === key); }
async function saveOutline(): Promise<void> {
  await act(async () => {
    if (!current.value || !outline.value) return;
    if (outline.value.chapters.some((item) => !item.title.trim()) || outline.value.scenes.some((item) => !item.title.trim())) throw new Error("章节和场景都需要一个可读名称。");
    if (outline.value.branches.some((item) => !item.label.trim() || (!item.target_scene && !item.ending_text.trim()))) throw new Error("分支需要选择文本；结束分支还需要玩家看得见的结局文字。");
    const stamp = epoch;
    const result = await client.saveOutline(current.value.id, current.value.project_hash, outline.value);
    if (!alive || stamp !== epoch) return;
    current.value = result.project; outline.value = copyOutline(result.project.outline!); dirty.value = false; retainPreviewAfterChange();
    await refreshProjects(); emit("changed"); publishSelection(); notice.value = "已保存新的作品版本。已有草稿的兼容状态将重新检查。";
  });
}
function removeScene(): void {
  if (!outline.value || !sceneId.value) return;
  const target = sceneId.value;
  if (outline.value.branches.some((item) => item.source_scene === target || item.target_scene === target) || outline.value.scenes.some((item) => item.dependencies.includes(target))) {
    error.value = "这个场景仍被分支或制作依赖引用。请先调整这些连接，再删除场景。"; return;
  }
  outline.value.scenes = outline.value.scenes.filter((item) => item.id !== target);
  if (outline.value.entry_scene_id === target) outline.value.entry_scene_id = outline.value.scenes[0]?.id ?? "";
  sceneId.value = outline.value.entry_scene_id; dirty.value = true; publishSelection();
}
async function bindArtifact(): Promise<void> {
  await act(async () => {
    const artifact = props.artifactSource;
    if (!current.value || !artifact || !sceneId.value || !props.canBind || dirty.value) return;
    const stamp = epoch;
    const result = await client.bindScene(current.value.id, current.value.project_hash, sceneId.value, artifact.artifact_id, artifact.artifact_hash);
    if (!alive || stamp !== epoch) return;
    current.value = result;
    retainPreviewAfterChange(); notice.value = `当前草稿已用于“${sceneName(sceneId.value)}”。${preview.value ? '先前试玩路径已保留，可检查并继承后继续。' : '可继续生成其他场景。'}`; emit("changed");
  });
}
function selectedScope(): SourceScope {
  if (sourceScope.value === "scene" && sceneId.value) return { scene_ids: [sceneId.value], chapter_ids: selectedScene.value ? [selectedScene.value.chapter_id] : [] };
  if (sourceScope.value === "chapter" && chapterId.value) return { chapter_ids: [chapterId.value] };
  return {};
}
async function uploadSource(): Promise<void> {
  await act(async () => {
    if (!current.value || !sourceName.value.trim() || !sourceText.value.trim()) throw new Error("请先填写来源名称和实际原文。");
    const stamp = epoch;
    const result = await client.addProjectSource(current.value.id, current.value.project_hash, { title: sourceName.value.trim(), text: sourceText.value, format: sourceFormat.value, status: sourceStatus.value,
      allowed_uses: ["drafting", "adaptation", ...(allowFacts.value ? ["facts"] : [])], scope: selectedScope(), original_locator: sourceLocator.value });
    if (!alive || stamp !== epoch) return;
    current.value = result.project; sources.value = [...sources.value, result.source]; factSourceId.value = result.source.id;
    if (attachSource.value && selectedScene.value) {
      const quote = Array.from(result.source.source.text).slice(0, 16000).join("");
      selectedScene.value.sources.push({ source_version_id: result.source.id, start: 0, end: Array.from(quote).length, quote }); dirty.value = true;
    }
    sourceName.value = ""; sourceText.value = ""; sourceLocator.value = ""; retainPreviewAfterChange(); emit("changed");
    notice.value = attachSource.value && selectedScene.value ? "资料已添加到当前场景。保存场景后用于生成。" : "资料已添加到资料库。";
  });
}
async function readFile(event: Event): Promise<void> {
  const file = (event.target as HTMLInputElement).files?.[0];
  if (!file) return;
  if (file.size > 4_000_000) { error.value = "本入口接收不超过4MB的文本类文件。"; return; }
  sourceText.value = await file.text(); sourceName.value ||= file.name; sourceLocator.value = file.name;
  const extension = file.name.split(".").at(-1)?.toLowerCase();
  sourceFormat.value = ({ md: "markdown", ink: "ink", yarn: "yarn", csv: "csv", tsv: "tsv", json: "json" } as Record<string, string>)[extension ?? ""] ?? "text";
}
async function recordFact(): Promise<void> {
  await act(async () => {
    const source = factSource.value; const occurrence = occurrences.value[quoteOccurrence.value];
    if (!current.value || !source || !occurrence) throw new Error("请引用在所选来源中实际存在的原句；重复句需要明确选择哪一处。");
    if (!factSubject.value.trim() || !factPredicate.value.trim() || !factValue.value.trim()) throw new Error("请填写事实的主体、关系与值。");
    const stamp = epoch;
    const result = await client.addClaim(current.value.id, current.value.project_hash, { subject: factSubject.value.trim(), predicate: factPredicate.value.trim(), value: factValue.value.trim(),
      anchors: [{ source_version_id: source.id, start: occurrence.start, end: occurrence.end, quote: factQuote.value }], scope: source.source.scope, interpretation: "author_statement" });
    if (!alive || stamp !== epoch) return;
    current.value = result.project; claims.value.push(result.claim); rejectIds.value[result.claim.id] = []; notice.value = "已保存设定候选，待确认。"; retainPreviewAfterChange();
    factSubject.value = ""; factPredicate.value = ""; factValue.value = ""; factQuote.value = ""; emit("changed");
  });
}
function conflicts(claim: NarrativeClaim): NarrativeClaim[] {
  return claims.value.filter((other) => other.id !== claim.id && other.decision?.accepted && other.claim.subject === claim.claim.subject && other.claim.predicate === claim.claim.predicate && JSON.stringify(other.claim.value) !== JSON.stringify(claim.claim.value));
}
function addVariable(): void {
  if (!outline.value) return;
  outline.value.variables.push({ id: `state_${crypto.randomUUID().slice(0, 8)}`, type: "bool", default: false, enum_values: [] }); dirty.value = true;
}
function variableTypeChanged(variable: ProjectOutline["variables"][number]): void {
  variable.default = variable.type === "bool" ? false : variable.type === "int" ? 0 : variable.enum_values[0] ?? ""; dirty.value = true;
}
function enumValuesChanged(variable: ProjectOutline["variables"][number], event: Event): void {
  variable.enum_values = [...new Set((event.target as HTMLInputElement).value.split(/[,，\n]/).map((item) => item.trim()).filter(Boolean))];
  if (!variable.enum_values.includes(String(variable.default))) variable.default = variable.enum_values[0] ?? "";
  dirty.value = true;
}
async function decideFact(claim: NarrativeClaim, accept: boolean): Promise<void> {
  await act(async () => {
    if (!current.value) return;
    const stamp = epoch;
    const result = await client.decideClaim(current.value.id, current.value.project_hash, claim.id, accept, rejectIds.value[claim.id] ?? []);
    if (!alive || stamp !== epoch) return;
    current.value = result.project;
    const detail = await client.project(current.value.id);
    if (!alive || stamp !== epoch) return;
    claims.value = detail.claims; retainPreviewAfterChange(); emit("changed");
    notice.value = accept ? "已在指定范围内采用此设定。" : "此设定未采用。";
  });
}
async function startPreview(): Promise<void> {
  await act(async () => {
    if (!current.value || dirty.value || pendingPreviewRead.value) return;
    const stamp = epoch;
    const result = await client.projectPreview(current.value.id, current.value.project_hash);
    if (alive && stamp === epoch) { pendingStep.value = null; adoptPreview(result); notice.value = "已从故事入口新开试玩；先前会话仍保留。"; }
  });
}
async function readPreview(previewId = preview.value?.preview_id): Promise<void> {
  await act(async () => {
    if (!current.value || !previewId) return;
    const stamp = epoch; const target = current.value.id;
    const result = await client.projectPreviewState(target, previewId);
    if (!alive || stamp !== epoch || current.value?.id !== target) return;
    adoptPreview(result);
    notice.value = previewNeedsRebase.value ? "已载入旧版路线，请检查兼容后继续。" : "已载入路线。";
  });
}
async function rebasePreview(): Promise<void> {
  await act(async () => {
    if (!current.value || !preview.value || fixedHistorical.value || dirty.value || pendingStep.value || pendingPreviewRead.value
      || !compatibilityCurrent.value || compatibility.value?.status !== "compatible") return;
    const stamp = epoch; const target = current.value.id; const source = preview.value.preview_id;
    try {
      const result = await client.rebaseProjectPreview(target, source, current.value.project_hash, preview.value.preview_hash);
      if (!alive || stamp !== epoch || current.value?.id !== target || preview.value?.preview_id !== source) return;
      pendingPreviewRead.value = { project_id: target, preview_id: result.preview_id,
        notice: result.snapshot_migration ? "已在新版继续，原路线与存档保留。" : "这条路线已属于当前版本，可以继续。" };
      storeValue(storageKey("active-preview", target), result.preview_id);
      await readDerivedPreview();
    } catch (failure) {
      if (alive && stamp === epoch) {
        compatibility.value = null; compatibilityScope.value = null;
        if (!pendingPreviewRead.value) previewIssue.value = `暂不能在新版继承：${humanizeError(failure)}。原路线与存档保留，可重新检查或沿旧版继续。`;
      }
      throw failure;
    }
  });
}
async function checkCompatibility(): Promise<void> {
  await act(async () => {
    if (!current.value || !preview.value || fixedHistorical.value || dirty.value || pendingStep.value || pendingPreviewRead.value) return;
    const stamp = epoch;
    const scope = { project_id: current.value.id, project_hash: current.value.project_hash, preview_id: preview.value.preview_id, preview_hash: preview.value.preview_hash };
    compatibility.value = null; compatibilityScope.value = null;
    const result = await client.projectPreviewCompatibility(scope.project_id, scope.preview_id, scope.project_hash, scope.preview_hash);
    if (!alive || stamp !== epoch || current.value?.id !== scope.project_id || current.value.project_hash !== scope.project_hash
      || preview.value?.preview_id !== scope.preview_id || preview.value.preview_hash !== scope.preview_hash) return;
    compatibility.value = result; compatibilityScope.value = scope; previewIssue.value = "";
  });
}
async function continueOriginal(): Promise<void> {
  await act(async () => {
    if (!current.value || !preview.value || fixedHistorical.value || dirty.value || pendingStep.value || pendingPreviewRead.value) return;
    const stamp = epoch; const target = current.value.id; const source = preview.value.preview_id;
    const result = await client.continueOriginalProject(target, source, current.value.project_hash, preview.value.preview_hash);
    if (!alive || stamp !== epoch || current.value?.id !== target || preview.value?.preview_id !== source) return;
    pendingPreviewRead.value = { project_id: target, preview_id: result.preview_id, notice: "已沿旧版另开试玩，不影响当前稿。" };
    storeValue(storageKey("active-preview", target), result.preview_id);
    await readDerivedPreview();
  });
}
async function readDerivedPreview(): Promise<void> {
  const pending = pendingPreviewRead.value;
  if (!pending || current.value?.id !== pending.project_id) return;
  const stamp = epoch;
  try {
    const latest = await client.projectPreviewState(pending.project_id, pending.preview_id);
    if (!alive || stamp !== epoch || current.value?.id !== pending.project_id
      || pendingPreviewRead.value?.preview_id !== pending.preview_id) return;
    if (latest.preview_id !== pending.preview_id || latest.project_id !== pending.project_id) throw new Error("返回的试玩身份与新会话不一致。");
    adoptPreview(latest); clearGenerationPath(); publishSelection(); notice.value = pending.notice;
  } catch (failure) {
    if (alive && stamp === epoch) previewIssue.value = `继续操作已完成，但最新试玩位置读取未完成：${humanizeError(failure)}。请读取会话，无需重复继续操作。`;
    throw failure;
  }
}
async function retryDerivedPreview(): Promise<void> { await act(readDerivedPreview); }
function editContinuation(): ApplicationContinuation | undefined {
  const source = props.artifactSource;
  const binding = current.value?.scene_bindings[sceneId.value];
  if (!current.value || dirty.value || !source || binding?.artifact_id !== source.artifact_id || binding?.artifact_hash !== source.artifact_hash) return undefined;
  return {
    narrative_project_id: current.value.id, narrative_scene_id: sceneId.value,
    narrative_project_hash: current.value.project_hash,
    ...(!fixedHistorical.value && preview.value?.project_id === current.value.id && preview.value.scene_id === sceneId.value && !previewNeedsRebase.value && !pendingStep.value && !pendingPreviewRead.value ? {
      project_preview_id: preview.value.preview_id, project_preview_hash: preview.value.preview_hash,
    } : {}),
  };
}
async function openEditContext(context: ApplicationContinuation): Promise<void> {
  if (!context.narrative_project_id || !context.narrative_scene_id) throw new Error("这份修改没有关联作品场景。");
  if (dirty.value) throw new Error("先保存或撤回当前结构编辑，再打开修改来源。");
  if (busy.value) throw new Error("正在处理当前作品，请稍后打开修改来源。");
  const stamp = ++epoch; const target = context.narrative_project_id; const targetScene = context.narrative_scene_id;
  busy.value = true; error.value = ""; notice.value = ""; projectId.value = target;
  try {
    await loadProject(context.project_preview_id ?? null);
    if (!alive || stamp !== epoch || current.value?.id !== target || projectId.value !== target) return;
    if (!outline.value?.scenes.some((scene) => scene.id === targetScene)) throw new Error("原场景已不在当前作品结构中；原试玩仍按其版本保留。");
    sceneId.value = targetScene;
    chapterId.value = outline.value.scenes.find((scene) => scene.id === targetScene)?.chapter_id ?? "";
    publishSelection();
    notice.value = context.project_preview_hash && preview.value && context.project_preview_hash !== preview.value.preview_hash
      ? "已打开来源场景。原试玩已更新，显示其最新保存位置。" : "已打开来源场景。";
  } catch (failure) {
    if (alive && stamp === epoch) { error.value = humanizeError(failure); throw failure; }
  } finally {
    if (alive && stamp === epoch) busy.value = false;
  }
}
defineExpose({ editContinuation, openEditContext });
async function step(choiceId?: string): Promise<void> {
  await act(async () => {
    if (!current.value || !preview.value || dirty.value || previewNeedsRebase.value || pendingPreviewRead.value) return;
    const transition = preview.value.view.status === "waiting_transition";
    const stamp = epoch;
    if (pendingStep.value && choiceId !== (pendingStep.value.choice_id ?? pendingStep.value.transition_id)) throw new Error("上一次选择尚未确认。请刷新路线，或重试同一次操作，避免改变已提交的选择。");
    const pending = pendingStep.value ?? { preview_id: preview.value.preview_id, action_id: crypto.randomUUID(), expected_seq: preview.value.action_seq, ...(choiceId ? transition ? { transition_id: choiceId } : { choice_id: choiceId } : {}) };
    pendingStep.value = pending;
    try {
      const { preview_id: target, ...input } = pending;
      const result = await client.projectStep(current.value.id, target, input);
      if (alive && stamp === epoch) { pendingStep.value = null; adoptPreview(result); }
    } catch (failure) {
      if (alive && stamp === epoch) previewIssue.value = "这次动作结果尚未确认；当前位置保留。先读取实际路线，或用同一个动作重试，不会自动选择另一条路。";
      throw failure;
    }
  });
}
async function saveArchive(): Promise<void> {
  await act(async () => {
    if (fixedHistorical.value) throw new Error("旧版隔离试玩保留在原会话中，不能存为当前作品的存档。");
    if (!current.value || !preview.value || !previewCanContinue.value || !archiveName.value.trim()) return;
    const stamp = epoch; const target = current.value.id;
    await client.archive(target, preview.value.preview_id, archiveName.value.trim(), preview.value.action_seq);
    if (!alive || stamp !== epoch) return;
    const result = await client.archives(target);
    if (!alive || stamp !== epoch) return;
    archives.value = result.archives; archiveName.value = ""; notice.value = "已保存当前路线和状态。";
  });
}
async function restore(archiveId: string): Promise<void> {
  await act(async () => {
    if (!current.value) return;
    const stamp = epoch;
    const operationKey = storageKey(`restore:${archiveId}:${current.value.project_hash}`);
    const operationId = storedValue(operationKey) || crypto.randomUUID(); storeValue(operationKey, operationId);
    let result: NarrativePreview;
    try { result = await client.restoreArchive(current.value.id, current.value.project_hash, archiveId, operationId); }
    catch (failure) { if (alive && stamp === epoch) previewIssue.value = `存档恢复尚未成功：${humanizeError(failure)}。原档与当前路线保留；再次点击会沿用同一次恢复操作。`; throw failure; }
    if (!alive || stamp !== epoch) return;
    storeValue(operationKey, ""); pendingStep.value = null; adoptPreview(result);
    notice.value = result.snapshot_migration ? "存档兼容检查通过，已恢复为当前版本的新试玩会话；原存档保持不变。" : "已恢复为新的试玩会话；原存档保持不变。";
  });
}
watch(sceneId, (value) => { if (generationSceneId.value && value !== generationSceneId.value) clearGenerationPath(); publishSelection(); });
watch(dirty, () => publishSelection());
watch(() => `${preview.value?.preview_id ?? ""}:${preview.value?.action_seq ?? ""}`, () => {
  if (generationPreviewId.value !== preview.value?.preview_id || generationSequence.value !== preview.value?.action_seq) clearGenerationPath();
  publishSelection();
});
watch(() => props.selectedProjectId, (value) => {
  if ((value ?? "") === projectId.value) return;
  epoch += 1; busy.value = false; compatibility.value = null; compatibilityScope.value = null;
  const stamp = epoch;
  projectId.value = value ?? ""; dirty.value = false; current.value = null; outline.value = null;
  void loadProject().catch((failure) => { if (alive && stamp === epoch) error.value = humanizeError(failure); });
});
watch(() => props.selectedSceneId, (value) => { if (value && outline.value?.scenes.some((scene) => scene.id === value)) sceneId.value = value; });
watch([chapterId, sceneSearch], () => { scenePage.value = 0; });
watch([factQuote, factSourceId], () => { quoteOccurrence.value = 0; });
watch(() => props.workspace, () => { epoch += 1; const stamp = epoch; busy.value = false; client = storyCreatorClient(props.workspace); current.value = null; outline.value = null; projectId.value = ""; preview.value = null; preservedPreviews.value = []; pendingStep.value = null; pendingPreviewRead.value = null; previewIssue.value = ""; compatibility.value = null; compatibilityScope.value = null; clearGenerationPath(); sources.value = []; claims.value = []; archives.value = []; publishSelection(); void refreshProjects().catch((failure) => { if (alive && stamp === epoch) error.value = humanizeError(failure); }); });
onMounted(() => { const stamp = epoch; void (async () => { await refreshProjects(); if (!alive || stamp !== epoch) return; if (props.selectedProjectId && !projectId.value) { projectId.value = props.selectedProjectId; await loadProject(); } })().catch((failure) => { if (alive && stamp === epoch) error.value = humanizeError(failure); }); });
onUnmounted(() => { alive = false; epoch += 1; });
</script>

<template>
  <section class="story-project">
    <header><div><span class="eyebrow">STORY PROJECT</span><h2>章节与场景</h2></div></header>
    <p v-if="error" class="feedback error" role="alert">{{ error }}</p><p v-if="notice" class="feedback notice" role="status">{{ notice }}</p>
    <div class="toolbar"><label>作品<select v-model="projectId" :disabled="busy || locked || dirty" @change="act(loadProject)"><option value="">独立草稿</option><option v-for="item in projects" :key="item.id" :value="item.id">{{ item.name }} · v{{ item.version }}</option></select></label><button :disabled="busy || locked || dirty" @click="act(async () => { await refreshProjects(); if (projectId) await loadProject(); })">刷新</button></div>
    <details v-if="!current"><summary>＋ 建立一部作品</summary><div class="create-row"><label>作品名称（必填）<input v-model="projectName" maxlength="200"></label><label>作品简介（选填）<input v-model="projectDescription"></label><button :disabled="busy || locked || !projectName.trim()" @click="createProject">建立作品</button></div></details>
    <template v-if="current && outline">
      <div class="project-heading"><p>{{ current.description || '尚未填写作品简介。' }}</p><span>已保存版本 {{ current.version }}{{ dirty ? ' · 结构尚有未保存编辑' : '' }}</span></div>
      <details class="outline-editor" :open="!current.outline"><summary>以章节与场景组织故事</summary><div class="outline-layout"><aside><label>章节<select v-model="chapterId"><option value="">所有章节</option><option v-for="chapter in outline.chapters" :key="chapter.id" :value="chapter.id">{{ chapter.title || '未命名章节' }}</option></select></label><button :disabled="busy || locked" @click="addChapter">＋ 章节</button><div v-if="selectedChapter" class="chapter-fields" @input="dirty = true"><label>章节名称<input v-model="selectedChapter.title"></label><label>章节摘要<textarea v-model="selectedChapter.summary" rows="2" /></label></div><label>查找场景<input v-model="sceneSearch" type="search"></label><div class="scene-list"><button v-for="scene in visibleScenes" :key="scene.id" :class="{ selected: scene.id === sceneId }" @click="sceneId = scene.id"><span>{{ scene.title || '未命名场景' }}</span><small>{{ current.scene_bindings[scene.id] ? '已有草稿' : '待生成' }}</small></button><p v-if="!sceneResults.length" class="hint">当前范围没有场景。</p></div><div v-if="sceneResults.length > 20" class="paging"><button :disabled="scenePage === 0" @click="scenePage -= 1">上一页</button><span>{{ scenePage + 1 }} / {{ Math.ceil(sceneResults.length / 20) }}</span><button :disabled="(scenePage + 1) * 20 >= sceneResults.length" @click="scenePage += 1">下一页</button></div><button :disabled="busy || locked || !chapterId" @click="addScene">＋ 当前章节的场景</button></aside>
        <div v-if="selectedScene" class="scene-editor" @input="dirty = true" @change="dirty = true"><div class="field-grid"><label>场景名称<input v-model="selectedScene.title"></label><label>所在章节<select v-model="selectedScene.chapter_id"><option v-for="chapter in outline.chapters" :key="chapter.id" :value="chapter.id">{{ chapter.title || '未命名章节' }}</option></select></label></div><label>这一场景发生什么<textarea v-model="selectedScene.brief" rows="3" /></label><div class="field-grid"><label>阅读顺序<input v-model.number="selectedScene.order" type="number" min="0"></label><label>汇流时如何处理状态<select v-model="selectedScene.merge_policy"><option value="preserve_path_state">沿用当前路线状态</option><option value="explicit_assignments">使用指定规则重设</option></select></label></div><p class="hint">汇合时沿用当前路线，不合并其他路线的状态；重设需填写规则。</p><h3>从这里可以去哪里？</h3><article v-for="branch in outgoing" :key="branch.id" class="branch-row"><label>选项文字<input v-model="branch.label"></label><label>去向<select v-model="branch.target_scene" @change="branch.ending_text = branch.target_scene ? '' : branch.ending_text"><option :value="null">到达一个结局</option><option v-for="scene in outline.scenes" :key="scene.id" :value="scene.id">{{ scene.title || '未命名场景' }}</option></select></label><label v-if="!branch.target_scene">玩家最终看见的结局<textarea v-model="branch.ending_text" rows="2" /></label><details><summary>可选：进入条件与状态后果</summary><label>进入条件（选填）<input v-model="branch.condition" :aria-describedby="`branch-condition-help-${branch.id}`"></label><p :id="`branch-condition-help-${branch.id}`" class="hint">使用已定义的状态标识编写表达式；留空为无条件。</p><details><summary>条件表达式示例</summary><p class="hint">若已定义整数状态 trust，可写 trust &gt;= 1。请使用本作品的实际状态标识。</p></details><div v-for="(effect, effectIndex) in branch.effects" :key="effectIndex" class="effect-row"><label>状态<select v-model="effect.var" @change="effectVariableChanged(effect)"><option v-for="variable in outline.variables" :key="variable.id" :value="variable.id">{{ variable.id }}</option></select></label><label>变化<select v-model="effect.op"><option value="set">设为</option><option v-if="variableFor(effect.var)?.type === 'int'" value="inc">增加</option><option v-if="variableFor(effect.var)?.type === 'int'" value="dec">减少</option></select></label><label>值<select v-if="variableFor(effect.var)?.type === 'bool'" v-model="effect.value" aria-label="状态值"><option :value="false">否</option><option :value="true">是</option></select><input v-else-if="variableFor(effect.var)?.type === 'int'" v-model.number="effect.value" type="number" step="1" aria-label="状态数值"><select v-else v-model="effect.value" aria-label="状态值"><option v-for="value in variableFor(effect.var)?.enum_values ?? []" :key="value" :value="value">{{ value }}</option></select></label><button class="text-link" @click="branch.effects.splice(effectIndex, 1); dirty = true">移除</button></div><button :disabled="busy || locked" @click="addEffect(branch)">＋ 这个选择的状态后果</button></details><button class="text-link" @click="outline.branches = outline.branches.filter((item) => item.id !== branch.id); dirty = true">移除此连接</button></article><button :disabled="busy || locked" @click="addBranch">＋ 一条选择或结局</button><details><summary>可选：前史依赖与场景条件</summary><div class="dependencies"><label v-for="scene in outline.scenes.filter((item) => item.id !== selectedScene!.id)" :key="scene.id" class="check"><input v-model="selectedScene.dependencies" type="checkbox" :value="scene.id">参考前史：“{{ scene.title || '未命名场景' }}”</label></div><label>进入条件（选填）<input v-model="selectedScene.precondition" aria-describedby="scene-conditions-format"></label><label>离开条件（选填）<input v-model="selectedScene.postcondition" aria-describedby="scene-conditions-format"></label><p id="scene-conditions-format" class="hint">使用已定义的状态标识；留空表示不限制。</p><p class="hint">已绑定{{ selectedScene.sources.length }}条来源片段。来源和前史变化后，需重新检查已有草稿。</p></details><button class="text-link" :disabled="busy || locked" @click="removeScene">删除没有连接引用的场景</button></div><p v-else class="hint">先添加章节，再添加场景。</p></div>
        <details class="project-variables" @input="dirty = true" @change="dirty = true"><summary>跨场景需要记住的状态（可选）</summary><p class="hint">在这里添加需要跨场景保留的状态。</p><article v-for="(variable, index) in outline.variables" :key="index" class="variable-row"><label>状态标识<input v-model="variable.id" :aria-describedby="`variable-id-format-${index}`"><small :id="`variable-id-format-${index}`">供条件和后果引用，命名后保持一致。</small></label><label>类型<select v-model="variable.type" @change="variableTypeChanged(variable)"><option value="bool">是／否</option><option value="int">整数</option><option value="enum">一组固定值</option></select></label><label v-if="variable.type === 'enum'">可选值，以逗号分隔<input :value="variable.enum_values.join('，')" @input="enumValuesChanged(variable, $event)"></label><label>初始值<select v-if="variable.type === 'bool'" v-model="variable.default"><option :value="false">否</option><option :value="true">是</option></select><input v-else-if="variable.type === 'int'" v-model.number="variable.default" type="number" step="1"><select v-else v-model="variable.default"><option v-for="value in variable.enum_values" :key="value" :value="value">{{ value }}</option></select></label><button class="text-link" @click="outline.variables.splice(index, 1); dirty = true">移除</button></article><button :disabled="busy || locked" @click="addVariable">＋ 需要记住的状态</button></details>
        <div class="outline-bottom"><label>故事入口<select v-model="outline.entry_scene_id" @change="dirty = true"><option value="">选择入口</option><option v-for="scene in outline.scenes" :key="scene.id" :value="scene.id">{{ scene.title || '未命名场景' }}</option></select></label><button class="primary" :disabled="busy || locked || !dirty" @click="saveOutline">保存结构为新版本</button><button :disabled="busy || locked || !dirty" @click="act(loadProject)">撤回尚未保存的结构编辑</button></div>
      </details>
      <div v-if="selectedScene" class="bind-scene"><div><strong>当前制作单元：{{ sceneName(sceneId) }}</strong><p class="hint">{{ current.scene_bindings[sceneId] ? '此场景已有草稿。替换后将保存为新的作品版本。' : '请先为此场景生成草稿，才能试玩。' }}</p></div><button :disabled="busy || locked || dirty || !canBind || !artifactSource" @click="bindArtifact">将当前草稿用于此场景</button></div>
      <details class="source-ledger"><summary>资料与设定 · {{ sources.length }}份材料 / {{ claims.length }}项事实</summary><p class="hint">资料按下方指定的范围和用途使用。</p><div class="ledger-layout"><section><h3>添加资料</h3><label>资料名称（必填）<input v-model="sourceName"></label><div class="field-grid"><label>格式<select v-model="sourceFormat"><option value="text">纯文本</option><option value="markdown">Markdown文稿</option><option value="ink">Ink脚本</option><option value="yarn">Yarn脚本</option><option value="articy_json">articy JSON</option><option value="json">通用JSON资料</option><option value="csv">CSV表格</option><option value="tsv">TSV表格</option></select></label><label>来源状态<select v-model="sourceStatus"><option value="inspiration">仅作灵感</option><option value="draft">项目草稿</option><option value="canon">作为已采用设定</option></select></label></div><label>导入文件（选填）<input aria-describedby="project-file-limit" type="file" accept=".txt,.md,.ink,.yarn,.json,.csv,.tsv" aria-label="读取文本类资料文件" @change="readFile"></label><p id="project-file-limit" class="hint">仅导入文本资料，最大4MB。</p><label>资料正文（必填）<textarea v-model="sourceText" rows="5" /></label><label>有效范围<select v-model="sourceScope"><option value="project">整个作品</option><option value="chapter" :disabled="!chapterId">当前章节</option><option value="scene" :disabled="!sceneId">当前场景</option></select></label><label class="check"><input v-model="allowFacts" type="checkbox">允许从资料中提取设定候选</label><label class="check"><input v-model="attachSource" type="checkbox" :disabled="!selectedScene">将前16000个字符内的片段用于当前场景</label><button :disabled="busy || locked || !sourceName.trim() || !sourceText.trim()" @click="uploadSource">添加资料</button><ul class="source-versions"><li v-for="source in sources" :key="source.id"><strong>{{ source.source.title }}</strong><span>v{{ source.version }} · {{ source.source.status === 'canon' ? '明确采用的设定来源' : source.source.status === 'draft' ? '草稿' : '灵感或停用来源' }} · {{ source.source.format }}</span><details><summary>查看原文</summary><p class="source-quote">{{ source.source.text.slice(0, 4000) }}</p><p v-if="source.source.text.length > 4000" class="hint">这里显示前4000字；完整原文已保存在资料库。</p></details></li></ul></section>
        <section><h3>从原文添加设定</h3><label>依据来源<select v-model="factSourceId"><option value="">选择来源</option><option v-for="source in sources" :key="source.id" :value="source.id">{{ source.source.title }} · v{{ source.version }}</option></select></label><label>原句摘录（必填）<textarea v-model="factQuote" rows="2" maxlength="16000" /></label><label v-if="occurrences.length > 1">同句出现多次，引用哪一处<select v-model.number="quoteOccurrence"><option v-for="(match, index) in occurrences" :key="index" :value="index">第{{ index + 1 }}处：{{ match.context }}</option></select></label><p v-else-if="factQuote && !occurrences.length" class="hint">尚未在所选原文中找到这句。</p><div class="field-grid"><label>设定主体<input v-model="factSubject"></label><label>关系<input v-model="factPredicate"></label></div><label>设定内容<input v-model="factValue"></label><button :disabled="busy || locked || !occurrences.length" @click="recordFact">保存设定候选</button><article v-for="claim in claims" :key="claim.id" class="claim"><span class="fact-status">{{ claim.decision ? claim.decision.accepted ? '已采用' : '未采用' : '待确认' }}</span><p><strong>{{ claim.claim.subject }}</strong> · {{ claim.claim.predicate }} · {{ valueText(claim.claim.value) }}</p><blockquote v-for="anchor in claim.claim.anchors" :key="`${anchor.source_version_id}:${anchor.start}`">{{ anchor.quote }}</blockquote><div v-if="conflicts(claim).length"><p class="hint">采用后需替换哪些旧设定？请选择；系统会检查生效范围是否冲突。</p><label v-for="other in conflicts(claim)" :key="other.id" class="check"><input v-model="rejectIds[claim.id]" type="checkbox" :value="other.id">同时不再采用：{{ valueText(other.claim.value) }}</label></div><div class="toolbar"><button :disabled="busy || locked" @click="decideFact(claim, true)">采用此设定</button><button :disabled="busy || locked" @click="decideFact(claim, false)">不采用</button></div></article></section></div></details>
      <details class="project-player" :open="Boolean(preview) || Boolean(previewIssue)">
        <summary>跨场景试玩与存档</summary>
        <div v-if="preview" class="snapshot-bar" role="status">
          <div><strong>{{ fixedHistorical ? '旧版隔离试玩' : previewNeedsRebase ? '先前路线已保留 · 待检查兼容' : '当前版本的试玩路线' }}</strong><p class="hint">{{ fixedHistorical ? '沿旧版正文与状态继续；重新试玩会从当前稿入口开始。来源撤权仍会限制推进。' : previewNeedsRebase ? '作品已更新。先核对已推进内容、前史与来源，再选择在新版继续或沿旧版试玩。' : '重新试玩将从入口开始，当前路线另行保留。' }}</p></div>
          <button v-if="previewNeedsRebase" :disabled="busy || locked || dirty || Boolean(pendingStep) || Boolean(pendingPreviewRead)" @click="checkCompatibility">检查新版兼容性</button>
          <button v-if="previewNeedsRebase" :disabled="busy || locked || dirty || Boolean(pendingStep) || Boolean(pendingPreviewRead)" @click="continueOriginal">沿旧版另开试玩</button>
          <button :disabled="busy || locked" @click="readPreview()">刷新路线</button>
        </div>
        <section v-if="previewNeedsRebase && compatibilityCurrent && compatibility" class="compatibility-result" role="status">
          <h3>{{ compatibility.status === 'compatible' ? '可以在新版继承这条路线' : '暂不能在新版继承' }}</h3>
          <ul v-if="compatibility.reasons.length"><li v-for="reason in compatibility.reasons" :key="reason">{{ reason }}</li></ul>
          <p class="hint">仅核对已记录的版本、推进内容、前史与来源，不判断剧情含义是否一致。继续时会再次检查。</p>
          <button class="primary" :disabled="busy || locked || dirty || Boolean(pendingStep) || Boolean(pendingPreviewRead) || compatibility.status !== 'compatible'" @click="rebasePreview">在新版继续</button>
          <details><summary>查看兼容依据</summary><pre>{{ JSON.stringify(compatibility.evidence, null, 2) }}</pre></details>
        </section>
        <p v-if="previewIssue" class="feedback error" role="alert">{{ previewIssue }}</p>
        <div v-if="pendingPreviewRead" class="pending-action"><p class="hint">继续操作已完成，尚未载入最新试玩位置。</p><button :disabled="busy || locked" @click="retryDerivedPreview">读取试玩会话</button></div>
        <p v-if="legacyPlayerRecord" class="hint">旧版试玩记录，可重新试玩使用新版呈现。</p>
        <div v-if="pendingStep" class="pending-action"><p class="hint">上次操作结果尚未确认。先刷新路线，或重试同一次操作。</p><button :disabled="busy || locked || dirty || previewNeedsRebase" @click="step(pendingStep.choice_id ?? pendingStep.transition_id)">重试同一次操作</button></div>
        <p v-if="preview?.snapshot_migration" class="migration-receipt" role="status">✓ 已完成兼容继承：保留{{ preview.coverage.visited_scene_count }}个已访问场景的状态和路线；原{{ preview.snapshot_migration.source_ref.kind === 'archive' ? '存档' : '会话' }}保持不变。</p>
        <div class="play-layout">
          <StoryPlaytest :preview="playable" :quest="previewBinding?.payload.quest" :tree="previewBinding?.payload.tree" :busy="busy || locked" :step-disabled="!previewCanContinue" :can-start="Boolean(current.outline) && !dirty && !pendingPreviewRead" :feedback-enabled="false" @start="startPreview" @step="step" @feedback="notice = '请回到此场景的创作任务填写修改意见。'" />
          <aside>
            <div v-if="preview && previewCanContinue" class="generation-path"><button :disabled="busy || locked || fixedHistorical" @click="usePlayedPath()">按当前路线生成此场景</button><template v-if="preview.view.status === 'waiting_transition'"><button v-for="choice in preview.view.choices.filter((choice) => choice.target_scene)" :key="choice.id" :disabled="busy || locked || fixedHistorical || choice.enabled === false || choice.available === false" @click="usePlayedPath(choice)">沿“{{ choice.label }}”制作下一场景 →</button></template><p v-if="fixedHistorical" class="hint">旧版隔离路线不用于当前稿生成或存档。</p><p v-else-if="generationPreviewId" class="hint">下次生成将参考已选路线的状态与前史。</p></div>
            <p v-if="preview" class="hint">当前场景：{{ previewSceneName(preview.scene_id) }}<br>已访问{{ preview.coverage.visited_scene_count }} / 共{{ preview.coverage.declared_scene_count }}个场景（本条路线）。</p>
            <ol v-if="preview"><li v-for="(key, index) in preview.visited_scenes.slice(-100)" :key="index">{{ previewNeedsRebase || fixedHistorical ? `第${index + 1}个历史场景` : sceneName(key) }}</li></ol>
            <details v-if="preservedPreviews.length" class="previous-routes"><summary>此前保留的路线 · {{ preservedPreviews.length }}条</summary><button v-for="(saved, index) in preservedPreviews" :key="saved.preview_id" :disabled="busy || locked" @click="readPreview(saved.preview_id)">读取第{{ index + 1 }}条 · 已访问{{ saved.coverage.visited_scene_count }}个场景 / {{ saved.action_seq }}步</button><p class="hint">切换路线将回到对应的保存位置。</p></details>
            <label>存档名称（必填）<input v-model="archiveName" :disabled="busy || locked || fixedHistorical"></label>
            <button :disabled="busy || locked || fixedHistorical || !preview || !previewCanContinue || !archiveName.trim()" @click="saveArchive">保存当前路径</button>
            <h3>存档</h3>
            <div v-for="archive in archives" :key="archive.id" class="archive"><span>{{ archive.name }}<small>{{ archive.project_hash === current.project_hash ? '当前作品版本' : '旧作品版本 · 可检查兼容恢复' }}</small></span><button :disabled="busy || locked || dirty" @click="restore(archive.id)">{{ archive.project_hash === current.project_hash ? '恢复' : '检查兼容并恢复' }}</button></div>
            <p v-if="!archives.length" class="hint">暂无存档。恢复时将另开会话，原存档保留。</p>
          </aside>
        </div>
      </details>
    </template>
  </section>
</template>

<style scoped>
.story-project { border: 1px solid var(--ow-paper-edge); border-radius: 12px; background: linear-gradient(125deg,rgba(40,34,48,.9),rgba(24,24,42,.92)); padding: 1.4rem; min-width: 0; }
header { display: flex; align-items: center; justify-content: space-between; gap: 1rem; flex-wrap: wrap; margin-bottom: 1rem; }h2 { font-size: 1.15rem; margin: .3rem 0 0; }h3 { font-size: .88rem; margin: 1rem 0 .5rem; }.eyebrow { font-size: .63rem; color: var(--ow-gold); letter-spacing: .15em; }.small-note,.hint { color: var(--ow-ink-mute); font-size: .73rem; line-height: 1.85; }.toolbar,.create-row,.outline-bottom { display: flex; flex-wrap: wrap; gap: .6rem; align-items: flex-end; }.toolbar label,.create-row label { flex: 1; min-width: 12rem; }.toolbar button,.create-row button { margin-bottom: .65rem; }
label { display: grid; gap: .4rem; margin: .65rem 0; color: var(--ow-ink-soft); font-size: .77rem; }button,input,select,textarea { font: inherit; color: inherit; }button { background: rgba(255,255,255,.025); border: 1px solid var(--ow-paper-edge); border-radius: 6px; padding: .55rem .7rem; font-size: .73rem; cursor: pointer; }button:hover:not(:disabled) { background: var(--ow-gold-faint); border-color: var(--ow-gold); }button:disabled { opacity: .42; cursor: default; }input,select,textarea { min-width: 0; width: 100%; background: rgba(12,12,25,.25); border: 1px solid var(--ow-paper-edge); border-radius: 5px; padding: .55rem .65rem; }textarea { line-height: 1.85; resize: vertical; }.primary { background: var(--ow-gold); color: var(--ow-night-deep); font-weight: 600; }.check { display: flex; align-items: flex-start; gap: .5rem; line-height: 1.75; }.check input { width: auto; margin-top: .3rem; }.feedback { padding: .75rem; border-radius: 6px; font-size: .8rem; line-height: 1.8; }.error { color: #e7a89b; background: rgba(200,85,61,.1); }.notice { color: #b5d5c4; background: rgba(115,168,133,.09); }
details { margin: .9rem 0; border-top: 1px solid var(--ow-paper-edge); padding-top: .7rem; }summary { cursor: pointer; color: var(--ow-ink-soft); font-size: .8rem; line-height: 1.8; }.project-heading { display: flex; align-items: center; justify-content: space-between; gap: 1rem; }.project-heading p { font-size: .82rem; line-height: 1.8; color: var(--ow-ink-soft); }.project-heading > span { font-size: .67rem; color: var(--ow-gold); }.outline-layout { display: grid; grid-template-columns: minmax(150px,.6fr) minmax(0,1.4fr); gap: 1.1rem; margin-top: .8rem; }.outline-layout > aside { padding-right: 1rem; border-right: 1px solid var(--ow-paper-edge); }.scene-list { max-height: 20rem; overflow: auto; display: grid; gap: .4rem; margin: .6rem 0; }.scene-list button { text-align: left; }.scene-list .selected { background: var(--ow-gold-faint); border-color: var(--ow-gold); }.scene-list small { display: block; color: var(--ow-ink-mute); font-size: .65rem; margin-top: .25rem; }.variable-row,.effect-row { display: flex; flex-wrap: wrap; gap: .6rem; align-items: flex-end; }.variable-row label { flex: 1; min-width: 7rem; }.variable-row > button { margin-bottom: .65rem; }.effect-row { margin: .7rem 0; }.effect-row label { flex: 1; min-width: 5rem; margin: 0; } .effect-row select,.effect-row input { width: 100%; min-width: 0; }.field-grid { display: grid; grid-template-columns: 1fr 1fr; gap: .7rem; }.branch-row { border: 1px solid var(--ow-paper-edge); border-radius: 7px; padding: .2rem .85rem .75rem; margin: .7rem 0; }.branch-row details { margin: .5rem 0; }.text-link { border: 0; background: transparent; color: var(--ow-cyan); padding: .25rem 0; font-size: .69rem; }.dependencies { max-height: 12rem; overflow: auto; }.outline-bottom { margin-top: 1rem; }.outline-bottom label { flex: 1; }.outline-bottom button { margin-bottom: .65rem; }.paging { display: flex; align-items: center; gap: .4rem; font-size: .65rem; margin-bottom: .6rem; }.paging button { padding: .35rem .45rem; font-size: .64rem; }.bind-scene { margin: 1rem 0; display: flex; align-items: center; gap: 1rem; padding: .8rem; border-radius: 7px; background: var(--ow-gold-faint); }.bind-scene > div { flex: 1; }.bind-scene strong { font-size: .8rem; }.bind-scene p { margin-bottom: 0; }
.ledger-layout,.play-layout { display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; margin-top: 1rem; min-width: 0; }.ledger-layout > section { min-width: 0; }.source-versions { padding: 0; list-style: none; }.source-versions > li { padding: .7rem 0; border-bottom: 1px solid rgba(255,255,255,.06); }.source-versions strong { font-size: .78rem; }.source-versions span { display: block; font-size: .65rem; color: var(--ow-ink-mute); margin-top: .3rem; }.source-quote { font-size: .75rem; line-height: 1.85; white-space: pre-wrap; max-height: 15rem; overflow: auto; color: var(--ow-ink-soft); }.claim { padding: 1rem 0; border-bottom: 1px solid var(--ow-paper-edge); }.claim p { font-size: .8rem; line-height: 1.85; }.fact-status { color: var(--ow-gold); font-size: .65rem; }blockquote { margin: .5rem 0; border-left: 2px solid var(--ow-gold-soft); padding-left: .7rem; color: var(--ow-ink-mute); font-size: .73rem; line-height: 1.8; }.play-layout > aside { border-left: 1px solid var(--ow-paper-edge); padding-left: 1rem; }.play-layout ol { color: var(--ow-ink-mute); font-size: .75rem; line-height: 1.85; }.generation-path { display: grid; gap: .5rem; margin-bottom: 1rem; }.generation-path button { text-align: left; border-color: var(--ow-gold-soft); }.archive { display: flex; align-items: center; justify-content: space-between; gap: .5rem; margin: .5rem 0; font-size: .75rem; }
.snapshot-bar { display: flex; flex-wrap: wrap; align-items: center; gap: .65rem; margin: 1rem 0; padding: .8rem; border: 1px solid var(--ow-gold-soft); border-radius: 7px; }.snapshot-bar > div { flex: 1; min-width: 14rem; }.snapshot-bar strong { color: var(--ow-gold); font-size: .8rem; }.snapshot-bar p { margin-bottom: 0; }.migration-receipt { color: #b5d5c4; font-size: .77rem; line-height: 1.8; }.pending-action { display: flex; align-items: center; flex-wrap: wrap; gap: .7rem; }.pending-action p { flex: 1; }.previous-routes button { display: block; width: 100%; text-align: left; margin: .45rem 0; }.archive small { display: block; margin-top: .25rem; font-size: .64rem; color: var(--ow-ink-mute); }
.compatibility-result { border: 1px solid var(--ow-paper-edge); border-radius: 7px; padding: .9rem; margin: .8rem 0; }.compatibility-result h3 { margin-top: 0; }.compatibility-result li { font-size: .77rem; line-height: 1.85; overflow-wrap: anywhere; }.compatibility-result pre { max-height: 20rem; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; font-size: .68rem; line-height: 1.7; }
@media (max-width: 880px) { .outline-layout,.ledger-layout,.play-layout { grid-template-columns: minmax(0,1fr); }.outline-layout > aside,.play-layout > aside { border: 0; padding: 0; }.scene-list { max-height: 11rem; }.bind-scene { flex-wrap: wrap; }.field-grid { grid-template-columns: minmax(0,1fr); gap: 0; } }
</style>
